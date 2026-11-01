"""最新观测：拉取研究区最近一景可用卫星影像，算当前湖泊状态。

--------------------------------------------------------------------------
为什么叫「最新可用观测」，不叫「实时」
--------------------------------------------------------------------------
Sentinel-2 的物理上限摆在那里，实测（2026-11-02，Kyagar）：

    * 最新一景 2026-10-28，距今 5 天，云量 2.2%
    * 近 120 天 64 景，其中云量 < 20% 的只有 10 景（16%）
    * 重访周期 5 天（双星组网）+ L2A 处理 1–3 天

所以能做到的是「准实时」：**每 5 天一次，且要天气配合**。
把这件事叫"实时"是承诺了做不到的东西，与项目命名时的取舍一致
（见 docs/命名说明.md）。本模块的每条结果都带 ``age_days``，
界面上必须把"这景是几天前的"显示出来。

--------------------------------------------------------------------------
判水口径：与 timeseries 完全一致，不另立一套
--------------------------------------------------------------------------
``NDWI > 0.02`` 且 ``NIR < 0.20``，再用 ``NDSI > 0.4 且 NIR > 0.11`` 的雪掩膜剔除冰雪。

这不是随手写的数：它是 ``timeseries.py`` 里已经跑了多年、并做过精度评价的
那套口径（召回 61.8%，见 docs §7.8）。本模块**直接 import 那些常数**，
不复制一份——复制的后果是两边悄悄漂移，而"当前面积 vs 历史序列"正是
这个功能的全部意义所在。

--------------------------------------------------------------------------
选景口径
--------------------------------------------------------------------------
1. 检索最近 ``lookback_days`` 天，云量上限 ``max_cloud``；
2. 按**日期倒序**取，同一天优先云量低的；
3. 对候选逐个尝试读取，第一个**完整覆盖**目标窗口的采用；
4. 覆盖合格后再过**窗口质量闸门**：NIR 中位数过低（整窗异常）或
   积雪占比过高（整窗雪盖）的候选同样跳过，换下一个；
5. 一个都读不通/不合格就如实返回 None，不拿窄条或假湖凑数。

先比覆盖、再比云量——与 build_series 的 ``(valid_px, area_km2)``
同一套理由：瓦片接缝处的窄条云量再低也没有湖（实测可低到 44% 覆盖）。

--------------------------------------------------------------------------
为什么只看云量不够：0.2% 云量的景能算出 36 km² 假湖
--------------------------------------------------------------------------
2026-11-02 实测（Kyagar 窗口）：2026-10-28 的 T43SFV 整景云量只有 6.5%，
但它窗口内 SCL 中位数 = 11（SNOW/ICE），B03/B08/B11 反射率中位数都是
0.0128，min/max 恰好 0/255——等于 0–255 的均匀噪声，没有物理意义。
直接跑 NDWI 得到 36.38 km²，而整个窗口才约 36 km²，即整窗都是假水。

云量是**整景**统计，既不代表窗口内无云，更不代表辐射值可信。所以必须有
一道看窗口内**光谱**的质量闸门（见 ``MIN_NIR_MEDIAN`` / ``MAX_SNOW_FRAC``）。

--------------------------------------------------------------------------
「没量到」与「量到零」必须分开
--------------------------------------------------------------------------
帕米尔 10 月以后湖面结冰，NDWI 在冰面上没有物理意义。此时：

    * ``is_frozen`` 为 True
    * ``total_area_km2`` 为 **None**（不是 0.0）
    * ``season_warning`` 说明原因

返回 0.0 会被下游当成"量到了，这个湖是空的"，与全项目的
「缺数据返回 None」约定一致（见 CLAUDE.md）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Sequence

import numpy as np

from . import io as gio
from . import masks
from . import stac as gstac
from .indices import S2_L2A_6BAND, ndsi, ndwi
from .timeseries import NDWI_THRESHOLD, NIR_MAX, SNOW_NDSI_THRESHOLD, SERIES_BANDS
from .water import LakeRecord, extract_lakes

__all__ = [
    "LatestObservation",
    "fetch_latest",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MAX_CLOUD",
    "MIN_COVERAGE_FRAC",
    "FROZEN_NDWI_MAX",
    "MIN_NIR_MEDIAN",
    "MAX_SNOW_FRAC",
]

#: 往前找多少天。重访周期 5 天，取 20 天给"连续多云"留余量
#: （实测 120 天里云量 <20% 的只有 10 景，即平均 12 天才有一景可用）。
DEFAULT_LOOKBACK_DAYS = 20

#: 整景云量上限（%）。帕米尔云多，20% 已偏严；实测能用的景只占 16%。
DEFAULT_MAX_CLOUD = 20.0

#: 窗口覆盖率下限。低于此值说明这景只擦到目标区一条边，
#: 面积会系统性偏小，不能当"当前状态"用。
MIN_COVERAGE_FRAC = 0.95

#: 窗口内 NDWI 最大值低于此值，判为"没有水信号"（冰封或已排空）。
#: 实测消融期湖面 NDWI 在 0.3–0.8，冰封期掉到 0 附近，两者差一个量级，
#: 0.05 这个门限落在中间，不会误判。
FROZEN_NDWI_MAX = 0.05

#: 窗口内 NIR 反射率中位数下限。低于此值判为"整窗都是水"，
#: 在冰川谷地不可能发生，只可能是辐射值坏了（噪声、未定标、产品缺陷）。
#:
#: 实测（2026-11-02，Kyagar 窗口，反射率 0–1 口径）：
#:
#:     * 正常景 2024-08-14 / 2025-09-13：NIR 中位数 0.419 / 0.402；
#:       跨 2023–2025 年 37 景完整覆盖样本，最低 0.392、中位 0.448
#:       （雪 0.6–0.9、裸岩 0.25–0.45、植被 0.25+，都远高于 0.05）
#:     * 异常景 2026-10-28 T43SFV / T43SGV：NIR 中位数 0.0128，且
#:       min/max = 0.0001/0.0255（0–255 均匀噪声），NDWI 因此算出 36.38 km²
#:
#: 0.05 落在两簇之间一个数量级以上，留足余量，不会误伤正常景。
MIN_NIR_MEDIAN = 0.05

#: 窗口内雪掩膜像元占比上限。超过就判为"整窗被雪盖住"——雪面 NDSI 与
#: 水体相近、SWIR 又低，面积统计会系统性失真，不能当"当前湖面"用。
#:
#: 实测（2026-11-02，同上窗口，``snow_mask(NDSI>0.4 且 NIR>0.11)`` 占比）：
#:
#:     * 正常景（37 景完整覆盖，2023-06 → 2025-10）：最低 0.201、
#:       中位 0.327、90% 分位 0.445、最高 0.828
#:     * 消融期（6–9 月）正常景：0.201–0.259，中位约 0.21
#:     * 隆冬 2025-03-17/03-22：0.828 / 0.801，整窗雪盖
#:     * 异常景 2026-10-28：0.000 —— 噪声让 NDSI≈0，雪掩膜拦不住它，
#:       那一景是靠 ``MIN_NIR_MEDIAN`` 拒掉的
#:
#: 0.60 取在消融期正常景（≤0.26）与隆冬整窗雪盖（≥0.80）之间。不用更低：
#: 10 月、11 月初的正常景已经到 0.25–0.45（谷地两侧山坡有雪是常态），
#: 阈值压到 0.5 会误伤这些合法的非消融期观测。
MAX_SNOW_FRAC = 0.60

#: 最多尝试几个候选景。每个候选要拉几 MB，试太多会等很久。
MAX_CANDIDATES = 4


@dataclass
class LatestObservation:
    """一次"当前状态"观测的完整记录。

    Attributes
    ----------
    observed_on
        影像**拍摄日期**（不是下载日期）。
    age_days
        距今多少天。本模块诚实性的核心字段，界面上必须显示。
    scene_id, tile, cloud
        场景标识与整景云量，供回溯。
    coverage
        窗口被这景覆盖的比例（1.0 = 完整）。
    records
        提取出的湖泊，按面积降序。
    total_area_km2
        全部水体面积合计。**None 表示没量到**（冰封/无水信号），不是 0。
    largest_area_km2
        最大单湖面积。同上，None 表示没量到。
    ndwi_max
        窗口内 NDWI 最大值。是解释"为什么没量到"的旁证。
    season_warning
        非消融期提示；空串表示无异常。
    """

    observed_on: str
    age_days: int
    scene_id: str
    tile: str
    cloud: float
    coverage: float
    records: list[LakeRecord] = field(default_factory=list)
    total_area_km2: float | None = None
    largest_area_km2: float | None = None
    ndwi_max: float = float("nan")
    season_warning: str = ""

    @property
    def n_lakes(self) -> int:
        return len(self.records)

    @property
    def is_frozen(self) -> bool:
        """窗口内几乎没有水信号 —— 冰封或已排空。"""
        return not (math.isfinite(self.ndwi_max) and self.ndwi_max >= FROZEN_NDWI_MAX)

    @property
    def usable(self) -> bool:
        """这条观测能不能拿来判"现在湖有多大"。"""
        return self.total_area_km2 is not None


def _coverage(scene, bbox: Sequence[float]) -> float:
    """窗口相对请求 bbox 的覆盖面积比（1.0 = 完整覆盖）。"""
    import rasterio
    from rasterio.warp import transform_bounds

    h, w = scene.shape
    got = rasterio.transform.array_bounds(h, w, scene.transform)
    want = transform_bounds("EPSG:4326", scene.crs, *[float(v) for v in bbox])
    ow = min(got[2], want[2]) - max(got[0], want[0])
    oh = min(got[3], want[3]) - max(got[1], want[1])
    full = (want[2] - want[0]) * (want[3] - want[1])
    return max(0.0, ow * oh) / full if full > 0 else 0.0


def _window_quality(nir, snow) -> tuple[str | None, float, float]:
    """窗口质量闸门。返回 ``(拒因, NIR 中位数, 雪占比)``，合格时拒因为 None。

    判据见 ``MIN_NIR_MEDIAN`` / ``MAX_SNOW_FRAC`` 的注释。两个数都从窗口内
    **有效像元**算，NaN 不参与——与 timeseries 的 valid_px 口径一致。

    注意：这两个判据是"整窗"级别的。云量大但没盖住窗口的景仍然合格，
    这不是遗漏——整景云量本来就是粗筛，窗口内逐像元的问题由雪掩膜和
    NIR 上限（``NIR_MAX``）在提取阶段处理。
    """
    finite_nir = nir[np.isfinite(nir)]
    nir_median = float(np.median(finite_nir)) if finite_nir.size else float("nan")

    if not math.isfinite(nir_median) or nir_median < MIN_NIR_MEDIAN:
        return "辐射值异常", nir_median, float("nan")

    snow_frac = float(snow.mean()) if snow.size else 0.0
    if snow_frac > MAX_SNOW_FRAC:
        return "积雪覆盖", nir_median, snow_frac

    return None, nir_median, snow_frac


def _season_warning(observed: date) -> str:
    """非消融期提示。判据与 build_series 的 6–9 月窗口一致。"""
    if observed.month in (6, 7, 8, 9):
        return ""
    return (
        f"观测日期 {observed.month} 月不在消融期（6–9 月），"
        f"帕米尔湖面可能已结冰，NDWI 失效"
    )


def fetch_latest(
    bbox: Sequence[float],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_cloud: float = DEFAULT_MAX_CLOUD,
    min_area_km2: float = 0.01,
    today: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> LatestObservation | None:
    """拉取研究区最近一景可用影像并提取当前湖泊。

    Parameters
    ----------
    bbox
        ``(lon_min, lat_min, lon_max, lat_max)``，WGS84。
    lookback_days
        往前检索多少天。
    max_cloud
        整景云量上限（%）。
    min_area_km2
        最小面积门限，与产品默认一致。判水阈值不在这里暴露——
        它固定取自 ``timeseries``，保证与历史序列同口径。
    today
        基准日期，默认今天。显式传入是为了让测试可复现——
        依赖 ``date.today()`` 的测试第二天就会挂。
    progress
        可选回调，签名 ``progress(message)``。

    Returns
    -------
    LatestObservation | None
        None 表示最近 ``lookback_days`` 天内没有一景满足条件。
        **调用方必须把 None 显示成"暂无可用观测"，不能当成"没有湖"。**
    """
    def emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    today = today or date.today()
    start = today - timedelta(days=int(lookback_days))
    emit(f"检索 {start.isoformat()} → {today.isoformat()} 的影像")

    try:
        refs = gstac.search_sentinel2(
            bbox, start.isoformat(), today.isoformat(),
            max_cloud=max_cloud, limit=100,
        )
    except Exception as exc:  # noqa: BLE001 - 检索失败就是没有观测，不炸调用方
        emit(f"检索失败：{type(exc).__name__}")
        return None

    if not refs:
        emit(f"最近 {lookback_days} 天无云量 <{max_cloud:g}% 的影像")
        return None

    # 先按云量升序，再按日期倒序做稳定排序 —— 结果是"日期新优先，
    # 同一天里云量低的优先"。分两步写比一个复合 key 更难读错。
    refs = sorted(refs, key=lambda r: r.cloud_cover)
    refs = sorted(refs, key=lambda r: r.date, reverse=True)
    emit(f"命中 {len(refs)} 景，按日期倒序尝试")

    rejected: list[str] = []
    for ref in refs[:MAX_CANDIDATES]:
        try:
            sources, headers = gstac.sign_scene(ref, bands=SERIES_BANDS)
            scene = gio.load_scene(
                sources, S2_L2A_6BAND, bounds=bbox, http_headers=headers or None,
            )
        except Exception as exc:  # noqa: BLE001 - 单景读不通就换下一景
            emit(f"  {ref.date} 读取失败（{type(exc).__name__}），换下一景")
            continue

        cov = _coverage(scene, bbox)
        if cov < MIN_COVERAGE_FRAC:
            emit(f"  {ref.date} 只覆盖 {cov:.0%}，跳过")
            rejected.append("覆盖不足")
            continue

        green, nir = scene["green"], scene["nir"]
        n = ndwi(green, nir)
        snow = masks.snow_mask(
            ndsi(green, scene["swir1"]), nir, threshold=SNOW_NDSI_THRESHOLD,
        )

        # 质量闸门：云量低不代表窗口可用（见模块文档的 36 km² 假湖）。
        # 拒掉就换下一个候选，与覆盖不足同等对待——契约里 fetch_latest
        # 只有"观测"和 None 两种结果，没有"质量不合格的观测"。
        reason, nir_median, snow_frac = _window_quality(nir, snow)
        if reason == "辐射值异常":
            emit(f"  {ref.date} NIR 中位数 {nir_median:.4f} < {MIN_NIR_MEDIAN:g}，"
                 f"窗口辐射值异常，跳过")
            rejected.append(reason)
            continue
        if reason == "积雪覆盖":
            emit(f"  {ref.date} 积雪覆盖 {snow_frac:.0%}，跳过")
            rejected.append(reason)
            continue

        finite = n[np.isfinite(n)]
        ndwi_max = float(finite.max()) if finite.size else float("nan")

        observed = date.fromisoformat(ref.date)
        warn = _season_warning(observed)

        # 只有"确实量到了"才给面积；量不到一律 None。
        #
        # 两种量不到：
        #   1. 窗口内没有水信号（NDWI 最大值 < 0.05）——冰封、云遮、湖已排空；
        #   2. 有水信号但一条湖也没提出（< min_area_km2 的碎斑不算）——
        #      说明水信号弱于判水口径（NDWI>0.02 且 NIR<0.20），没有可信的水体。
        #
        # 第 2 种为什么不能给 0.0：窗口在冰川谷地里，窗口内"一个湖都没有"
        # 与"湖空了"在数据上无法区分，而后者是我们想知道的、前者只是没看见。
        # 写 0.0 等于替用户断言"湖没了"，这正是 CLAUDE.md 里
        # 「把没量到写成 0 比缺数据更危险」说的那种情况。
        # 实测例：2026-10-23 那景 NDWI 最大 0.311（>0.05，过第一道闸），
        # 但提取 0 条——10 月底湖面已结冰，此时 0.0 会被误读成"湖干了"。
        records: list[LakeRecord] = []
        total: float | None = None
        largest: float | None = None
        if math.isfinite(ndwi_max) and ndwi_max >= FROZEN_NDWI_MAX:
            records = extract_lakes(
                n, scene.transform, scene.crs,
                threshold=NDWI_THRESHOLD, min_area_km2=min_area_km2,
                exclude=snow, nir=nir, nir_max=NIR_MAX,
            )
            if records:
                total = sum(r.area_km2 for r in records)
                largest = max(r.area_km2 for r in records)

        return LatestObservation(
            observed_on=ref.date,
            age_days=(today - observed).days,
            scene_id=ref.id,
            tile=ref.tile,
            cloud=ref.cloud_cover,
            coverage=cov,
            records=records,
            total_area_km2=total,
            largest_area_km2=largest,
            ndwi_max=ndwi_max,
            season_warning=warn,
        )

    # 拒因要如实说出来：用户看到"暂无可用观测"时，得知道是没景、只擦边、
    # 还是整窗被雪盖住——这三种情况对"要不要人工去看一眼"的判断完全不同。
    n_tried = min(len(refs), MAX_CANDIDATES)
    if rejected:
        counts: dict[str, int] = {}
        for reason in rejected:
            counts[reason] = counts.get(reason, 0) + 1
        if len(counts) == 1:
            reason, n = next(iter(counts.items()))
            emit(f"{n} 个候选均因{reason}被跳过")
        else:
            detail = "、".join(f"{n} 个{reason}" for reason, n in counts.items())
            emit(f"{n_tried} 个候选均不合格（{detail}）")
    else:
        emit(f"前 {n_tried} 个候选都读不通")
    return None
