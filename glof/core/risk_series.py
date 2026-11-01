"""把溃决风险分级接到面积时序上：读 CSV、留出法取参照、逐期判定。

这一层回答的问题是：**手里有一条面积时序（CSV），怎么判出每期的风险等级？**
单期判定在 ``risk.py`` 里（每条判据独立输出、无权重），但"拿哪几期当参照"
是个系列政策问题，单独放在这里，界面和 CLI 共用同一个口径。

--------------------------------------------------------------------------
留出法（不要改）
--------------------------------------------------------------------------
用 ``assess_ice_dammed()`` 估平台面积时，**参照序列里绝不能包含待评估那一期**——
否则最大值就是它自己，比值恒为 1，判据自动命中，这是循环论证
（``risk.estimate_plateau_area()`` 的 docstring 有完整警告）。

本模块的政策（``holdout_reference_areas()``），按优先级：

1. **跨年留出**：只用年份不同于待评估期的观测。2017 与 2018 两个独立年份
   估出的平台值差 0.0006 km²（跨年可复现，verify_risk 已验证），这是首选。
2. **年内回退**：跨年不足 3 期时，退而用**同一年里日期严格早于待评估期**的观测。
   充水是单调上涨过程，早期观测的上包络低于当前值，用它当平台会抬高比值——
   所以这个回退只在"今年就是唯一有数据的年份"时使用，并且政策说明里写明。
3. 都不足 3 期 → 返回空参照，``risk.assess_ice_dammed()`` 会判 UNKNOWN 并
   写明"历史序列不足 3 期"。缺数据判 UNKNOWN 而不是 LOW，是诚实性要求。

--------------------------------------------------------------------------
为什么取 max 当平台对"窄条"污染是稳健的
--------------------------------------------------------------------------
年际序列里混着 valid_px 只有 73902 的窄条观测（瓦片接缝，湖不在覆盖内，
面积严重偏小，如 2016-06-27 的 0.0767 km²）。平台取**最大值**，窄条只可能
把平台往小估、不可能往大估，所以窄条不会制造虚假 HIGH——但它们作为
**待评估期**时面积本身是假的，调用方应该用 ``min_valid_frac`` 先过滤。

--------------------------------------------------------------------------
冰碛坝的基准政策
--------------------------------------------------------------------------
冰碛坝判据之一是面积扩张速率，需要两期观测。政策：基准期默认取序列里
**最早的一期**（可用 baseline_date 显式指定）；基准期当期本身时间跨度为 0，
``risk.assess_moraine_dammed()`` 会判 UNKNOWN。坡度与母冰川接触没有数据源，
不传就是 UNKNOWN，绝不静默通过。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import risk as grisk
from .timeseries import SeriesPoint

#: 低于这个角度就认为"没量到坝体"：湖面本身是平的，用湖心当代替点必然接近 0。
#: 1° 是个宽松下限——真正的冰碛坝下游坡度在 10° 量级（risk.DEFAULT_THRESHOLDS
#: 的 downstream_slope_deg=10 就是按这个量级定的），量出来不足 1° 说明点位错了。
MIN_MEASURABLE_SLOPE_DEG = 1.0

__all__ = [
    "SeriesAssessment",
    "read_series_csv",
    "holdout_reference_areas",
    "assess_series",
]


@dataclass
class SeriesAssessment:
    """时序上一期的判定结果：观测点 + 风险判定 + 所用的参照政策说明。"""

    point: SeriesPoint
    assessment: grisk.RiskAssessment
    policy: str = ""

    @property
    def date(self) -> str:
        return self.point.date

    @property
    def area_km2(self) -> float:
        return self.point.area_km2

    @property
    def value(self) -> float | None:
        """第一条有数值的判据值（冰坝是面积/平台比值，冰碛坝是年增速）。"""
        for criterion in self.assessment.criteria:
            if isinstance(criterion.value, (int, float)) and not isinstance(criterion.value, bool):
                if criterion.value is not None and math.isfinite(float(criterion.value)):
                    return float(criterion.value)
        return None


def read_series_csv(path: str | Path) -> list[SeriesPoint]:
    """读时序 CSV，兼容本仓库两种落盘格式。

    必需列：``date``、``area_km2``。其余列缺席时给安全默认值：
    年际格式（kyagar_area_series.csv：year,date,tile,cloud,valid_px,area_km2,
    ndwi_max,scene）没有 valid_frac / water_frac；加密格式（kyagar_dense.csv）
    没有 ndwi_max。缺的列填 nan，不猜。文件带 BOM（utf-8-sig），用 csv 模块
    读并显式按 utf-8-sig 解码，否则表头第一个键会带上 ``\\ufeff`` 导致列名对不上。

    返回按日期升序的列表。缺 date / area_km2 或面积非数的行跳过并计告警，
    不让一行脏数据毁掉整条序列。
    """
    rows: list[SeriesPoint] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path} 是空文件，没有表头")
        missing = {"date", "area_km2"} - set(reader.fieldnames)
        if missing:
            raise ValueError(f"{path} 缺必需列 {sorted(missing)}，实际列：{reader.fieldnames}")
        for raw in reader:
            date = (raw.get("date") or "").strip()
            try:
                area = float(raw.get("area_km2", ""))
            except (TypeError, ValueError):
                continue
            if not date or not math.isfinite(area):
                continue

            def _float(name: str) -> float:
                try:
                    value = float(raw.get(name, ""))
                    return value if math.isfinite(value) else float("nan")
                except (TypeError, ValueError):
                    return float("nan")

            year_raw = (raw.get("year") or "").strip()
            year = int(year_raw) if year_raw.isdigit() else int(date[:4])
            valid_px_raw = _float("valid_px")
            rows.append(
                SeriesPoint(
                    date=date,
                    year=year,
                    area_km2=area,
                    valid_px=int(valid_px_raw) if math.isfinite(valid_px_raw) else 0,
                    valid_frac=_float("valid_frac"),
                    tile=(raw.get("tile") or "").strip(),
                    cloud=_float("cloud"),
                    scene=(raw.get("scene") or "").strip(),
                    ndwi_max=_float("ndwi_max"),
                    water_frac=_float("water_frac"),
                )
            )
    rows.sort(key=lambda p: p.date)
    return rows


def holdout_reference_areas(points: Sequence[SeriesPoint], date: str) -> tuple[list[float], str]:
    """给待评估的那一期取参照面积序列（留出法，绝不含它自己）。

    返回 ``(参照面积列表, 政策说明)``。政策见模块 docstring：
    先跨年，不足 3 期退年内早期，再不足返回空（上游会判 UNKNOWN）。
    """
    target_year = int(date[:4])
    other_years = [p.area_km2 for p in points if p.year != target_year and p.area_km2 > 0]
    if len(other_years) >= 3:
        return other_years, f"跨年留出：用 {len(other_years)} 个其他年份估平台"
    earlier_same_year = [
        p.area_km2 for p in points if p.year == target_year and p.date < date and p.area_km2 > 0
    ]
    if len(earlier_same_year) >= 3:
        return (
            earlier_same_year,
            f"年内回退：同年前 {len(earlier_same_year)} 期估平台（充水期上包络偏低，比值可能虚高）",
        )
    return [], "无可用参照：跨年与年内早期观测都不足 3 期"


def slope_from_dem(
    dem_url_or_path: str,
    lake_lat: float,
    lake_lon: float,
    *,
    pad_deg: float = 0.03,
    dem: np.ndarray | None = None,
    transform=None,
) -> float | None:
    """从 DEM 量坝体下游坡度，供 ``assess_series(downstream_slope_deg=...)``。

    为什么要单独一个函数：DEM 是外面的一层（网络或本地文件），
    而 risk_series 的其余部分是纯计算。把读取挡在这里，判定逻辑就能
    在无网络环境下跑。

    返回 None 的条件与 ``terrain.downstream_slope`` 一致：DEM 取不到、
    坝体在 DEM 外、或 500 m 剖面不完整。调用方据此让判据走 UNKNOWN，
    **不要把 None 换成 0**——那会把"没量"说成"量出来是平的"。

    Parameters
    ----------
    dem_url_or_path
        Copernicus 瓦片 URL 或本地 GeoTIFF。传 ``""`` 时使用 ``dem``/``transform``。
    lake_lat, lake_lon
        坝体位置。湖的质心是个近似——真实坝体在湖的下游端，
        这个偏差写进返回值的日志里，不装作是精确定位。
    """
    from . import terrain as gterr  # 延迟导入：只有真要读 DEM 时才拉起 rasterio

    if dem is None:
        if not dem_url_or_path:
            return None
        bbox = (lake_lon - pad_deg, lake_lat - pad_deg,
                lake_lon + pad_deg, lake_lat + pad_deg)
        try:
            dem, transform = gterr.read_dem_bounds(dem_url_or_path, bbox)
        except Exception:  # noqa: BLE001 - 取不到 DEM 是环境问题，不炸判定
            return None
    # 湖心落在湖面上，量出来的是"湖面平不平"，不是"坝体下游陡不陡"。
    # 实测克亚吉尔湖心：高程 4769 m，四周 500 m 只有 +10~+250 m 的岸坡，
    # 量得 -0.01°；而同一窗口内真实坡地可达 52°（中位 19°）。
    # 这个数值交给 risk 会被当成"量测成功、坡度为零"，比缺数据更危险，
    # 所以近水平的结果一律退回 None，走 UNKNOWN。
    slope = gterr.downstream_slope(dem, transform, lake_lat, lake_lon)
    if slope is None:
        return None
    if abs(slope) < MIN_MEASURABLE_SLOPE_DEG:
        return None
    return slope


def assess_series(
    points: Sequence[SeriesPoint],
    lake_type: str,
    *,
    date: str | None = None,
    baseline_date: str | None = None,
    downstream_slope_deg: float | None = None,
    glacier_contact: bool | None = None,
    thresholds: Mapping[str, float] | None = None,
    min_valid_frac: float = 0.0,
) -> list[SeriesAssessment]:
    """对时序逐期做风险判定。

    Parameters
    ----------
    points
        ``read_series_csv()`` 的输出，或界面侧 ``build_series()`` 的结果。
    lake_type
        坝型，走 ``risk.assess_lake`` 的别名表（ice / moraine …）。
    date
        只评估这一期（必须存在于序列）。缺省评估全部期。
    baseline_date
        冰碛坝基准期（必须存在于序列）。缺省用最早一期。
    downstream_slope_deg, glacier_contact
        冰碛坝判据的可选输入；不传就是缺数据 → UNKNOWN。
    thresholds
        传给 ``risk.assess_lake`` 的阈值覆盖。
    min_valid_frac
        过滤 valid_frac 低于该值的期（窄条）。valid_frac 为 nan（CSV 没这列）时
        不过滤——没法判断覆盖质量的观测直接丢掉会掩盖问题，交给报告里的
        valid_px 列让人看见。

    Returns
    -------
    list[SeriesAssessment]
        按日期升序。``date`` 指定的期不存在时抛 KeyError。
    """
    selected = [p for p in points if p.valid_frac != p.valid_frac or p.valid_frac >= min_valid_frac]
    if date is not None:
        chosen = [p for p in selected if p.date == date]
        if not chosen:
            raise KeyError(f"序列里没有 {date} 这一期（共 {len(selected)} 期可选）")
        targets = chosen
    else:
        targets = selected

    key = grisk.lake_type_kind(lake_type)
    out: list[SeriesAssessment] = []

    if key == "ice":
        for point in targets:
            reference, policy = holdout_reference_areas(selected, point.date)
            assessment = grisk.assess_ice_dammed(
                point.area_km2,
                reference_areas=reference or None,
                thresholds=thresholds,
                label=point.date,
            )
            out.append(SeriesAssessment(point=point, assessment=assessment, policy=policy))
        return out

    if key == "moraine":
        base = None
        if baseline_date is not None:
            base = next((p for p in selected if p.date == baseline_date), None)
            if base is None:
                raise KeyError(f"序列里没有基准期 {baseline_date}")
        elif selected:
            base = selected[0]
        for point in targets:
            if base is None:
                assessment = grisk.assess_moraine_dammed(
                    point.area_km2, thresholds=thresholds, label=point.date
                )
                policy = "序列为空，无基准期"
            else:
                assessment = grisk.assess_moraine_dammed(
                    point.area_km2,
                    baseline_area_km2=base.area_km2,
                    baseline_year=float(base.year),
                    year=float(point.year),
                    downstream_slope_deg=downstream_slope_deg,
                    glacier_contact=glacier_contact,
                    thresholds=thresholds,
                    label=point.date,
                )
                policy = f"基准期 {base.date}（{base.area_km2:.4f} km²）"
            out.append(SeriesAssessment(point=point, assessment=assessment, policy=policy))
        return out

    # 坝型无法识别：每期都给一条 UNKNOWN，理由写在 summary 里，不让 CLI 静默空转。
    note = (
        f"坝型 {lake_type!r} 无法识别，判据无从选择：冰坝看面积/平台比值，"
        f"冰碛坝看扩张速率与坝体条件，两者不能混用"
    )
    for point in targets:
        out.append(
            SeriesAssessment(
                point=point,
                assessment=grisk.RiskAssessment(
                    level=grisk.RiskLevel.UNKNOWN,
                    dam_type="unknown",
                    criteria=[],
                    summary=note,
                ),
                policy="坝型未识别",
            )
        )
    return out


def series_sensitivity_cases(
    points: Sequence[SeriesPoint],
    lake_type: str,
    *,
    downstream_slope_deg: float | None = None,
    glacier_contact: bool | None = None,
) -> list[dict[str, Any]]:
    """把时序转成 ``risk.sensitivity()`` 的 cases（逐期一个用例）。

    与 ``assess_series()`` 用同一套参照政策，保证敏感性分析扰动的是
    同一条判定路径。
    """
    key = grisk.lake_type_kind(lake_type)
    cases: list[dict[str, Any]] = []
    if key == "ice":
        for point in points:
            reference, _policy = holdout_reference_areas(points, point.date)
            if reference:
                cases.append(
                    dict(
                        lake_type="ice",
                        area_km2=point.area_km2,
                        reference_areas=reference,
                        label=point.date,
                    )
                )
        return cases
    if key == "moraine" and points:
        base = points[0]
        for point in points:
            cases.append(
                dict(
                    lake_type="moraine",
                    area_km2=point.area_km2,
                    baseline_area_km2=base.area_km2,
                    baseline_year=float(base.year),
                    year=float(point.year),
                    downstream_slope_deg=downstream_slope_deg,
                    glacier_contact=glacier_contact,
                    label=point.date,
                )
            )
    return cases
