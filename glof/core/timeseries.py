"""时序提取：把一个点位在若干年/若干期上的湖泊面积算成一条序列。

这一层是从 `scripts/kyagar_series.py`（年际）和 `scripts/kyagar_dense.py`（年内加密）
里抽出来的。两个脚本只是"时间轴不同"，取数、判水、选景的规则完全一样，
所以规则集中放在这里，界面直接调 `build_series()` 就能拿到同样口径的结果。

--------------------------------------------------------------------------
为什么按 valid_px 选景，而不是按云量
--------------------------------------------------------------------------
克亚吉尔正好压在 T43SFV / T43SGV 两景瓦片的接缝上。有一景整景云量只有 0.2%，
但它在 T43SGV 上只覆盖 AOI 的一条 16 像元宽的窄缝——湖整个落在窄缝外面。
按云量选，就会选中这一景，面积算出来是 0。

所以选景的第一关键字必须是 **窗口内有效像元数 valid_px**，
第二关键字才是面积（`(valid_px, area_km2)` 元组比较，与改造前的脚本一致）。
云量只作为检索时的粗筛（`max_cloud`）和报表里的参考，不参与选景。

--------------------------------------------------------------------------
为什么 valid_frac 不够，必须同时报 valid_px
--------------------------------------------------------------------------
`valid_frac` 是"窗口内有效像元 / 窗口总像元"。它对瓦片裁剪**不敏感**：
窄条内部每个像元都是有效的，所以窄条的 valid_frac 也是 100%。
2018-08-26 那一期就是典型——valid_frac=1.0 看起来很完美，
但 valid_px 只有 73902，而完整覆盖是 378324，说明只有 20% 的窗口真的被观测到。
只报 valid_frac 会让人以为这一期质量很好，实际上湖根本不在覆盖范围内。
两个数一起看才能发现"窄条"问题。

--------------------------------------------------------------------------
判水口径（不要改）
--------------------------------------------------------------------------
    NDWI > 0.02 且 NIR < 0.20，再用 NDSI>0.4 且 NIR>0.11 的雪掩膜剔掉冰雪。

这套参数是实测最优（召回 61.8%，见 docs §7.8）。改阈值会让整条序列口径漂移，
与已经发表/已经跑出来的结果对不上。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import numpy as np

from . import io as gio
from . import masks
from . import risk as grisk
from . import stac as gstac
from .indices import S2_L2A_6BAND, ndsi, ndwi
from .water import build_water_mask

__all__ = [
    "SeriesPoint",
    "build_series",
    "estimate_plateau",
    "group_by_year",
]

# 判水口径：实测最优，见模块文档与 docs §7.8
NDWI_THRESHOLD = 0.02
NIR_MAX = 0.20
SNOW_NDSI_THRESHOLD = 0.4


@dataclass
class SeriesPoint:
    """时序上的一个观测点（同一日期只保留一景）。

    Attributes
    ----------
    date
        ``YYYY-MM-DD``。
    year
        观测年份，与 ``date`` 前四位一致，单独留一份方便分组。
    area_km2
        窗口内水体像元数 × 像元面积。注意这是**固定窗口**里的面积，
        窗口外的部分不算，所以 PAD 要够大（见脚本里的 PAD_DEG）。
    valid_px
        窗口内有效像元数。选景的第一关键字，也是识别"窄条"的唯一依据。
    valid_frac
        有效像元 / 窗口总像元。**不能单独看**——瓦片裁剪出来的窄条内部
        也是 100%，必须和 ``valid_px`` 一起判读。
    tile
        Sentinel-2 瓦片号（T43SFV / T43SGV …）。
    cloud
        整景云量（%），仅作参考，不参与选景。
    scene
        场景 ID，可回溯到具体那一景。
    ndwi_max
        窗口内 NDWI 最大值。湖完全不在窗口内时它会掉到 0.3 以下，
        是比面积更灵敏的"这一期到底有没有看到湖"的旁证。
    water_frac
        水体像元 / 有效像元。界面和 CSV 沿用改造前的列，所以一并保留。
    """

    date: str
    year: int
    area_km2: float
    valid_px: int
    valid_frac: float
    tile: str
    cloud: float
    scene: str
    ndwi_max: float
    water_frac: float = float("nan")


def _measure(ref: gstac.SceneRef, bbox: Sequence[float]) -> SeriesPoint:
    """读一景、算一期面积。失败时抛异常，由调用方决定是否跳过。"""
    sources, headers = gstac.sign_scene(ref)
    scene = gio.load_scene(sources, S2_L2A_6BAND, bounds=bbox, http_headers=headers or None)

    green, nir = scene["green"], scene["nir"]
    valid = int(np.isfinite(green).sum())

    n = ndwi(green, nir)
    s = ndsi(green, scene["swir1"])
    snow = masks.snow_mask(s, nir, SNOW_NDSI_THRESHOLD)
    water = build_water_mask(n, NDWI_THRESHOLD, exclude=snow, nir=nir, nir_max=NIR_MAX)

    water_px = int(water.sum())
    h, w = scene.shape
    finite = n[np.isfinite(n)]

    return SeriesPoint(
        date=ref.date,
        year=int(ref.date[:4]),
        area_km2=float(water_px * scene.pixel_area_m2 / 1e6),
        valid_px=valid,
        valid_frac=valid / float(h * w),
        tile=ref.tile,
        cloud=ref.cloud_cover,
        scene=ref.id,
        ndwi_max=float(finite.max()) if finite.size else float("nan"),
        water_frac=float(water_px / max(valid, 1)),
    )


def build_series(
    bbox: Sequence[float],
    years: Iterable[int],
    start_md: str = "06-01",
    end_md: str = "09-15",
    max_cloud: float = 60,
    per_year_limit: int = 60,
    progress: Callable[[str], None] | None = None,
) -> list[SeriesPoint]:
    """逐年检索 Sentinel-2，按日期返回每个日期最好的一景。

    同一日期可能在两个相邻瓦片各有一景，按日期分组后**用
    `(valid_px, area_km2)` 元组挑覆盖最完整的那景**——先比有效像元数，
    覆盖不足的景面积再大也是假的（详见模块文档）。

    Parameters
    ----------
    bbox
        ``(lon_min, lat_min, lon_max, lat_max)``，WGS84。窗口固定不变，
        所以各期面积可以直接相减比较。
    years
        要处理的年份，逐个检索。单年失败不影响其它年。
    start_md, end_md
        年内物候窗口 ``MM-DD``。帕米尔只做 6–9 月：其它月份湖面结冰，
        NDWI 失效，取到的"面积"没有物理意义。
    max_cloud
        检索时的整景云量上限（%）。**只是粗筛**，不用于选景。
    per_year_limit
        每年最多取几景。数值越大，同一日期命中两景的概率越高，
        单期成本也越高。
    progress
        可选回调，签名 ``progress(message: str)``，None 时静默。
        只在"检索失败 / 无可用影像 / 整日读取失败"这类需要人看一眼的事件上触发；
        正常观测通过返回值给出，避免界面与命令行重复打印同一行。

    Returns
    -------
    list[SeriesPoint]
        每个日期一个点，按日期升序。检索失败的年份不产生任何点。

    Notes
    -----
    这个函数会真的联网读 COG。年际 10 年 × 每年几景大约要跑几分钟，
    不要放在界面的主线程里同步调用。
    """
    def emit(message: str) -> None:
        if progress is not None:
            progress(message)

    points: list[SeriesPoint] = []

    for year in years:
        try:
            refs = gstac.search_sentinel2(
                bbox,
                f"{year}-{start_md}",
                f"{year}-{end_md}",
                max_cloud=max_cloud,
                limit=per_year_limit,
            )
        except Exception as exc:  # noqa: BLE001 - 单年失败不该中断整条序列
            emit(f"{year} 检索失败 {type(exc).__name__}")
            continue

        if not refs:
            emit(f"{year} 无可用影像")
            continue

        by_date: dict[str, list[gstac.SceneRef]] = {}
        for ref in refs:
            by_date.setdefault(ref.date, []).append(ref)

        for date in sorted(by_date):
            best: SeriesPoint | None = None
            for ref in by_date[date]:
                try:
                    point = _measure(ref, bbox)
                except Exception:  # noqa: BLE001 - 单景读失败（越界/缺资产）跳过
                    continue
                # 先比有效覆盖，再比面积：覆盖不足的景面积再大也是假的
                if best is None or (point.valid_px, point.area_km2) > (
                    best.valid_px,
                    best.area_km2,
                ):
                    best = point
            if best is None:
                emit(f"{date} 全部读取失败")
                continue
            points.append(best)

    points.sort(key=lambda p: p.date)
    return points


def estimate_plateau(points: Sequence[SeriesPoint], quantile: float = 1.0) -> float:
    """从时序估"满蓄平台值"（km²）。

    实现转发到 ``risk.estimate_plateau_area()``——平台面积的口径全项目只能有一个。
    那边的理由（平台是物理天花板，湖蓄到溢流口就停止上涨，观测值的上包络才是
    无偏估计；分位数会系统性低估平台、抬高比值、把没蓄满的湖判成高风险）
    已经由 ``scripts/verify_risk.py`` 用 2017/2018 两个独立年份的实测序列验证过
    （最大值 1.8254 vs 90% 分位 1.5031，后者低了 17.7%）。所以默认取最大值，
    ``quantile`` 只留给做敏感性分析时用。

    注意它默认吃**全部**点，包括云多的、覆盖不足的。如果序列里低质量期占比很高，
    调用方应先用 ``valid_px`` / ``valid_frac`` 过滤再传进来。
    有效观测不足 3 期时返回 ``nan``（单期或两期分不清"平台"还是"恰好赶上一次
    高水位"，拒绝给出会误导风险判定的数）。

    Returns
    -------
    float
        没有可用点或观测不足 3 期时返回 ``nan``。
    """
    areas = [p.area_km2 for p in points if np.isfinite(p.area_km2)]
    value = grisk.estimate_plateau_area(areas, quantile=quantile)
    return float("nan") if value is None else float(value)


def group_by_year(points: Sequence[SeriesPoint]) -> dict[int, list[SeriesPoint]]:
    """按年份分组，组内保持日期升序。"""
    out: dict[int, list[SeriesPoint]] = {}
    for point in sorted(points, key=lambda p: p.date):
        out.setdefault(point.year, []).append(point)
    return out
