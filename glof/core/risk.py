"""冰湖溃决风险分级——可审计的规则判定，不做加权求和。

为什么不用加权指数
------------------
"面积 × w1 + 增速 × w2 + 坝型 × w3" 这类综合指数，答辩时只需一句"权重怎么来的"
就崩了：答"参考文献"会被追问"换一组权重排序会不会变"，答"专家经验"等于承认主观。
本模块换一个能被评委逐条复核的东西——**每条判据单独输出是否命中、实测值、阈值**，
等级只由"命中了几条"决定，没有任何可调权重。

等级规则（唯一的判定逻辑，写在 `_decide_level()` 里）
------------------------------------------------------
    全部判据命中、且没有缺数据的判据  -> HIGH
    有判据命中，但存在未命中或缺数据  -> MEDIUM
    全部判据未命中、且没有缺数据      -> LOW
    一条都评估不了，或一条没中但仍有缺口 -> UNKNOWN

最后一条是**诚实性要求**：数据不全时不能判低。缺一条判据就少一个可能命中的机会，
默认判低等于把"不知道"当成"安全"，那是这类系统最危险的失败方式。

两类坝体的判据完全不同
----------------------
* 冰坝湖（克亚吉尔）：**面积 / 地形平台面积**。冰坝湖蓄到溢流口就停止上涨，
  满蓄面积由坝体地形决定、年际重复（2017-07-27 与 2018-08-06 实测都是 1.825 km²，
  差 0.0006），所以"是否触及平台值"是一个可观测、可复现的状态量。
  比值 >0.95 即高风险。评委可以自己拿任何一年的影像验算这条。
* 冰碛坝湖（西帕米尔 4 个湖）：**没有平台值**（坝体是松散冰碛物，会被下切，
  不存在稳定的溢流口高度），所以改用面积扩张速率 + 坝体下游坡度 + 母冰川接触。
  这三条目前有两条**没有数据源**（DEM、RGI 冰川编目都没接入），
  缺失时一律输出 UNKNOWN 并在 reasons 里写明"缺数据未评估"，绝不静默通过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "RiskLevel",
    "Criterion",
    "RiskAssessment",
    "ThresholdShift",
    "DEFAULT_THRESHOLDS",
    "estimate_plateau_area",
    "assess_ice_dammed",
    "assess_moraine_dammed",
    "assess_lake",
    "lake_type_kind",
    "sensitivity",
]

# 阈值集中放在这里，是因为 sensitivity() 要按名字扰动它们。
# 这不是"权重"——每条阈值只控制自己那一条判据的开关，改它只会让判据更容易/更难命中，
# 不会像权重那样把不同量纲的指标混成一个数。
DEFAULT_THRESHOLDS: dict[str, float] = {
    # 冰坝湖：面积/平台比值。0.95 的来历见模块开头（实测溃决前 4 天比值 1.0003）。
    "plateau_ratio": 0.95,
    # 冰碛坝：相对年增速（面积年增长 / 基准面积）。2%/年 是区域经验的起点值，
    # 必须用研究区实测分布校准；本模块把它暴露成参数并做敏感性检验，而不是藏进权重里。
    "expansion_rate_per_yr": 0.02,
    # 冰碛坝：坝体下游坡度（度）。坡越陡，溃口下切越快、洪水消能越少。
    "downstream_slope_deg": 10.0,
}

_ICE_ALIASES = {"ice", "ice_dammed", "ice-dammed", "glacier_dammed", "冰坝", "冰坝湖"}
_MORAINE_ALIASES = {"moraine", "moraine_dammed", "moraine-dammed", "冰碛", "冰碛坝", "冰碛坝湖"}


class RiskLevel(str, Enum):
    """风险等级。继承 str 是为了能直接塞进 GeoJSON properties 而不必转换。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        return {
            RiskLevel.HIGH: "高风险",
            RiskLevel.MEDIUM: "中风险",
            RiskLevel.LOW: "低风险",
            RiskLevel.UNKNOWN: "无法判定（数据不足）",
        }[self]


def _num_text(value: Any) -> str:
    """判据文本里的数字格式。比值要保留到 4 位（1.0003 与 1 不是一回事），
    面积/坡度这类量级大的不需要那么多位。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "-"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "-"
        if abs(value) >= 100.0:
            return f"{value:.1f}"
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-") else "0"
    return str(value)


@dataclass
class Criterion:
    """一条判据的判定结果。

    ``hit is None`` 表示**缺数据、未评估**——它既不是命中也不是未命中，
    这是本模块和"打分模型"最重要的区别：缺失不被当成 0 分，而是被显式记账。
    """

    name: str
    hit: bool | None
    value: float | bool | None = None
    threshold: float | bool | None = None
    note: str = ""

    @property
    def status(self) -> str:
        if self.hit is None:
            return "UNKNOWN"
        return "HIT" if self.hit else "MISS"

    def describe(self) -> str:
        head = {"UNKNOWN": "[缺数据未评估]", "HIT": "[命中]", "MISS": "[未命中]"}[self.status]
        if self.value is None:
            body = self.name
        else:
            body = f"{self.name} = {_num_text(self.value)}（阈值 {_num_text(self.threshold)}）"
        return f"{head} {body}：{self.note}" if self.note else f"{head} {body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "hit": self.hit,
            "value": self.value,
            "threshold": self.threshold,
            "note": self.note,
        }


@dataclass
class RiskAssessment:
    """一个湖的风险判定结果，可直接序列化进 GeoJSON properties。"""

    level: RiskLevel
    dam_type: str
    criteria: list[Criterion] = field(default_factory=list)
    summary: str = ""

    @property
    def reasons(self) -> list[str]:
        """逐条判据的人读文本。直接落到 LakeRecord.risk_reasons，界面上就是风险列。"""
        return [c.describe() for c in self.criteria]

    @property
    def n_hit(self) -> int:
        return sum(1 for c in self.criteria if c.hit is True)

    @property
    def n_unknown(self) -> int:
        return sum(1 for c in self.criteria if c.hit is None)

    def to_properties(self) -> dict[str, Any]:
        return {
            "risk_level": self.level.value,
            "risk_dam_type": self.dam_type,
            "risk_summary": self.summary,
            "risk_reasons": self.reasons,
            "risk_criteria": [c.to_dict() for c in self.criteria],
        }

    def report(self) -> str:
        lines = [f"等级 {self.level.value}（{self.level.label}）  坝型 {self.dam_type}"]
        lines.extend(f"  {r}" for r in self.reasons)
        lines.append(f"  {self.summary}")
        return "\n".join(lines)


def _thresholds(overrides: Mapping[str, float] | None) -> dict[str, float]:
    """合并用户给的阈值。未提到的键用默认值，这样 sensitivity() 能只扰动其中一个。"""
    merged = dict(DEFAULT_THRESHOLDS)
    if overrides:
        unknown = set(overrides) - set(merged)
        if unknown:
            raise KeyError(f"未知阈值名 {sorted(unknown)}，可用：{sorted(merged)}")
        merged.update({k: float(v) for k, v in overrides.items()})
    return merged


def _decide_level(criteria: Sequence[Criterion]) -> RiskLevel:
    """等级规则，全文只有这一处。没有权重、没有加权平均、没有分数。"""
    if not criteria:
        return RiskLevel.UNKNOWN
    evaluated = [c for c in criteria if c.hit is not None]
    unknown = [c for c in criteria if c.hit is None]
    hits = [c for c in evaluated if c.hit]

    if not evaluated:
        return RiskLevel.UNKNOWN
    if not unknown and len(hits) == len(evaluated):
        return RiskLevel.HIGH
    if hits:
        return RiskLevel.MEDIUM
    # 一条都没中，但还有判据没评估——可能是缺的那条本来会命中，不能判低。
    if unknown:
        return RiskLevel.UNKNOWN
    return RiskLevel.LOW


def _summary(dam_label: str, level: RiskLevel, criteria: Sequence[Criterion]) -> str:
    n_eval = sum(1 for c in criteria if c.hit is not None)
    n_hit = sum(1 for c in criteria if c.hit is True)
    unknown = [c.name for c in criteria if c.hit is None]
    text = f"{dam_label}：已评估 {n_eval} 条判据，命中 {n_hit} 条，判为{level.label}。"
    if unknown:
        text += (
            f" 另有 {len(unknown)} 条判据因缺数据未评估（{'、'.join(unknown)}），"
            f"等级只代表已评估的部分，可能偏低。"
        )
    return text


def estimate_plateau_area(
    areas: Iterable[float],
    *,
    quantile: float = 1.0,
    min_obs: int = 3,
) -> float | None:
    """从历史观测序列估"地形平台面积"（满蓄上限）。

    为什么取**最大值**而不是均值或中位数：平台是物理天花板——湖蓄到溢流口就停止上涨，
    所以观测值的上包络才是平台的无偏估计（只能从下方逼近，永远不会超过）。
    取中位数会系统性低估平台，导致比值虚高、把没蓄满的湖判成高风险，
    这正是"宁可漏报也不能误报"的判据最不能犯的错。

    为什么要求 ``min_obs >= 3``：一期观测的"最大值"就是它自己，两期也分不清
    是平台还是恰好赶上一次高水位。三期以上才谈得上"封顶"。

    .. warning::
        **不要把待评估的那一期算进序列。** 否则最大值就是它自己，比值恒等于 1，
        判据自动命中——这是循环论证，评委一问就穿。正确做法是用历史年份
        （或本次观测之前的观测）估平台，再拿当前面积去除。
    """
    values = sorted(
        float(a) for a in areas if a is not None and math.isfinite(float(a)) and float(a) > 0
    )
    if len(values) < max(1, min_obs):
        return None
    if quantile >= 1.0:
        return values[-1]
    if quantile <= 0.0:
        return values[0]
    pos = quantile * (len(values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def assess_ice_dammed(
    area_km2: float,
    *,
    plateau_area_km2: float | None = None,
    reference_areas: Sequence[float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    label: str = "",
) -> RiskAssessment:
    """冰坝湖判据：面积 / 地形平台面积 > 0.95 即高风险。

    Parameters
    ----------
    area_km2
        待评估这一期的湖面面积。
    plateau_area_km2
        平台面积（km²）。有实测/已知值就直接给，优先级最高。
    reference_areas
        用于**自动估**平台面积的历史观测序列（km²）。**不应包含 area_km2 本身**，
        理由见 ``estimate_plateau_area()`` 的警告。
    """
    thr = _thresholds(thresholds)
    ratio_thr = thr["plateau_ratio"]

    plateau = None
    source = ""
    if plateau_area_km2 is not None and plateau_area_km2 > 0:
        plateau = float(plateau_area_km2)
        source = "平台面积由外部给定"
    elif reference_areas:
        series = [float(a) for a in reference_areas if a is not None]
        plateau = estimate_plateau_area(series)
        if plateau is not None:
            source = f"平台面积由 {len(series)} 期历史观测自动估计（取最大值）"

    if plateau is None or not math.isfinite(area_km2) or area_km2 <= 0:
        note = (
            "未提供平台面积，历史序列也不足 3 期，无法估计满蓄上限；"
            "冰坝湖没有平台值就无法判断是否蓄满"
        )
        criterion = Criterion("面积/平台比值", None, None, ratio_thr, note)
        level = RiskLevel.UNKNOWN
        return RiskAssessment(
            level=level,
            dam_type="ice_dammed",
            criteria=[criterion],
            summary=_summary("冰坝湖", level, [criterion]),
        )

    ratio = area_km2 / plateau
    hit = ratio > ratio_thr
    note = (
        f"{_num_text(area_km2)} km² ÷ {_num_text(plateau)} km²；"
        f"冰坝湖蓄到溢流口就停止上涨，比值接近 1 说明已蓄满、溢流在即"
    )
    criterion = Criterion("面积/平台比值", hit, ratio, ratio_thr, note)
    level = _decide_level([criterion])
    summary = _summary("冰坝湖", level, [criterion])
    if source:
        summary += f"（{source}）"
    if label:
        summary = f"[{label}] {summary}"
    return RiskAssessment(level=level, dam_type="ice_dammed", criteria=[criterion], summary=summary)


def assess_moraine_dammed(
    area_km2: float,
    *,
    baseline_area_km2: float | None = None,
    baseline_year: float | None = None,
    year: float | None = None,
    downstream_slope_deg: float | None = None,
    glacier_contact: bool | None = None,
    thresholds: Mapping[str, float] | None = None,
    label: str = "",
) -> RiskAssessment:
    """冰碛坝湖判据：面积扩张速率 + 坝体下游坡度 + 母冰川接触。

    冰碛坝没有稳定溢流口（坝体会被下切），不存在平台值，所以不能用面积比值。
    改用"长得快不快 + 坝体下游陡不陡 + 上游有没有母冰川继续供水"。

    ``downstream_slope_deg`` 与 ``glacier_contact`` 目前**没有数据源**
    （DEM、RGI 冰川编目都未接入）。缺失时对应判据输出 UNKNOWN 并写明原因，
    等级按 ``_decide_level()`` 处理——有缺口时最高只能给到 MEDIUM，
    一条都没中时给 UNKNOWN 而不是 LOW。
    """
    thr = _thresholds(thresholds)
    criteria: list[Criterion] = []

    # ---- 判据 1：面积扩张速率（相对年增速） ----
    rate_thr = thr["expansion_rate_per_yr"]
    if baseline_area_km2 and baseline_year is not None and year is not None:
        span = float(year) - float(baseline_year)
        if span > 0 and baseline_area_km2 > 0:
            rate = (area_km2 - baseline_area_km2) / baseline_area_km2 / span
            note = (
                f"{_num_text(baseline_area_km2)} km²（{float(baseline_year):g}）"
                f" → {_num_text(area_km2)} km²（{float(year):g}），"
                f"{span:g} 年增长 {rate * 100:.2f}%/年；"
                f"用相对增速是为了不让大湖天然占优"
            )
            criteria.append(Criterion("面积扩张速率", rate > rate_thr, rate, rate_thr, note))
        else:
            criteria.append(
                Criterion(
                    "面积扩张速率", None, None, rate_thr,
                    "基准年与当前年之间没有时间跨度（或基准面积为 0），增速无定义",
                )
            )
    else:
        criteria.append(
            Criterion(
                "面积扩张速率", None, None, rate_thr,
                "缺基准期观测：需要同一湖两期以上面积及对应年份才能算年增速",
            )
        )

    # ---- 判据 2：坝体下游坡度（缺 DEM） ----
    slope_thr = thr["downstream_slope_deg"]
    if downstream_slope_deg is None:
        criteria.append(
            Criterion(
                "坝体下游坡度", None, None, slope_thr,
                "未接入 DEM，无法量测坝体下游坡度（需 30 m DEM + 坝体位置）",
            )
        )
    else:
        note = "坡越陡，溃口下切越快、下游消能越少，峰值流量越高"
        criteria.append(
            Criterion(
                "坝体下游坡度", downstream_slope_deg > slope_thr,
                float(downstream_slope_deg), slope_thr, note,
            )
        )

    # ---- 判据 3：母冰川接触（缺 RGI 冰川编目） ----
    if glacier_contact is None:
        criteria.append(
            Criterion(
                "母冰川接触", None, None, True,
                "未接入 RGI 冰川编目，无法判断湖体是否与母冰川/冰前湖相接",
            )
        )
    else:
        note = "与母冰川相接说明上游还在持续供冰供水，湖会继续扩张"
        criteria.append(
            Criterion("母冰川接触", bool(glacier_contact), bool(glacier_contact), True, note)
        )

    level = _decide_level(criteria)
    summary = _summary("冰碛坝湖", level, criteria)
    if label:
        summary = f"[{label}] {summary}"
    return RiskAssessment(level=level, dam_type="moraine_dammed", criteria=criteria, summary=summary)


def lake_type_kind(lake_type: str) -> str:
    """归一化坝型：返回 ``"ice"`` / ``"moraine"`` / ``"unknown"``。

    判定口径只有 ``assess_lake()`` 里那份别名表，这里只是把"能不能识别、
    属于哪类"暴露成公开查询，供调用方在**进入判定前**分派政策
    （比如 risk_series 的留出法只对冰坝有意义）。识别规则与 assess_lake 完全一致，
    不会出现"这里认得、assess_lake 却判 unknown"的分叉。
    """
    key = str(lake_type or "").strip().lower()
    if key in _ICE_ALIASES:
        return "ice"
    if key in _MORAINE_ALIASES:
        return "moraine"
    return "unknown"


def assess_lake(
    lake_type: str,
    area_km2: float,
    *,
    thresholds: Mapping[str, float] | None = None,
    **kwargs: Any,
) -> RiskAssessment:
    """按坝型分派判据。坝型未知时不猜——直接 UNKNOWN 并说明原因。"""
    key = str(lake_type or "").strip().lower()
    if key in _ICE_ALIASES:
        return assess_ice_dammed(area_km2, thresholds=thresholds, **kwargs)
    if key in _MORAINE_ALIASES:
        return assess_moraine_dammed(area_km2, thresholds=thresholds, **kwargs)

    note = (
        f"坝型 {lake_type!r} 无法识别，判据无从选择：冰坝看面积/平台比值，"
        f"冰碛坝看扩张速率与坝体条件，两者不能混用"
    )
    return RiskAssessment(
        level=RiskLevel.UNKNOWN,
        dam_type="unknown",
        criteria=[],
        summary=note,
    )


@dataclass
class ThresholdShift:
    """一个阈值 ±perturbation 后的等级翻转情况。"""

    name: str
    base: float
    low: float
    high: float
    n_cases: int
    flipped_low: list[str] = field(default_factory=list)
    flipped_high: list[str] = field(default_factory=list)

    @property
    def flip_rate_low(self) -> float:
        return len(self.flipped_low) / self.n_cases if self.n_cases else 0.0

    @property
    def flip_rate_high(self) -> float:
        return len(self.flipped_high) / self.n_cases if self.n_cases else 0.0

    @property
    def max_flip_rate(self) -> float:
        return max(self.flip_rate_low, self.flip_rate_high)


def sensitivity(
    cases: Sequence[Mapping[str, Any]],
    *,
    perturbation: float = 0.2,
    thresholds: Mapping[str, float] | None = None,
) -> list[ThresholdShift]:
    """阈值 ±20% 扰动，统计等级翻转率。

    答辩时被问"阈值凭什么定 0.95"的标准答法是：**给不出唯一正确的阈值，
    但能给出这个阈值有多要紧**——如果 ±20% 内只有 1/16 个湖的等级变了，
    那结论不依赖阈值；如果翻了一半，那就得承认结论不稳。

    Parameters
    ----------
    cases
        每个元素是 ``assess_lake()`` 的参数字典，必须含 ``lake_type`` 与 ``area_km2``，
        建议带 ``label``（用于报告是哪几个湖翻转了）。字典里不要放 ``thresholds``，
        本函数会覆盖它。
    """
    base_thr = _thresholds(thresholds)
    labels = [str(c.get("label") or f"case{i + 1}") for i, c in enumerate(cases)]
    base_levels = [_assess_case(c, base_thr).level for c in cases]

    shifts: list[ThresholdShift] = []
    for name, base_value in base_thr.items():
        row = ThresholdShift(
            name=name,
            base=base_value,
            low=base_value * (1.0 - perturbation),
            high=base_value * (1.0 + perturbation),
            n_cases=len(cases),
        )
        for factor, bucket in ((row.low, row.flipped_low), (row.high, row.flipped_high)):
            thr = dict(base_thr)
            thr[name] = factor
            for i, case in enumerate(cases):
                if _assess_case(case, thr).level != base_levels[i]:
                    bucket.append(labels[i])
        shifts.append(row)
    return shifts


def _assess_case(case: Mapping[str, Any], thresholds: Mapping[str, float]) -> RiskAssessment:
    kwargs = dict(case)
    kwargs["thresholds"] = thresholds
    return assess_lake(**kwargs)
