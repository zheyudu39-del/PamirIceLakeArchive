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

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import numpy as np
import rasterio

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


#: 时序只读这 3 个波段。算 NDWI 要 green+nir，冰雪掩膜要 green+swir1，
#: blue 与 swir2 **在这个流程里完全没用到**。原先按 6 波段读，白拉 1/3 的流量。
#: 实测单景 6 波段要 9–30 s（瓶颈是下行带宽，见 docs/时序检索为什么慢.md），
#: 减到 3 个能省掉其中约一半。
#:
#: 注意：改这个元组会改变 Scene 的 key 集合，_measure 里只能取这三项。
SERIES_BANDS = ("green", "nir", "swir1")

#: 读远程 COG 时的 GDAL 选项。HTTP/2 让同景多个波段共用一条连接，
#: 把三次 TLS 握手摊成一次。实测（2026-11-02，12 个日期并发 10 路）
#: 14.34 s → 11.41 s，1.26 倍，且不改变任何数值结果。
#: 只对远程生效——本地文件传这些选项无害，GDAL 会忽略。
_HTTP_ENV = {
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MULTIPLEX": "YES",
}


def _geometric_coverage(ref: gstac.SceneRef, bbox: Sequence[float]) -> float:
    """用 STAC item 的 bbox 估这景对目标窗口的覆盖比（0–1）。

    为什么能用它预筛：Sentinel-2 的 item bbox 是**整幅瓦片的经纬度包络**，
    而瓦片是规则网格，包络与真实覆盖高度一致。实测 Kyagar 窗口上
    T43SFV 算出 1.00、T43SGV 算出 0.42，与读像素后得到的
    valid_frac（1.00 vs 0.20）方向一致。

    它是**粗筛**不是精确值：包络会略微高估（经纬度度数与投影面积不是线性
    关系），所以调用方仍以实际读到的 ``valid_frac`` 为准。这里只用来排序，
    决定先读哪一景。
    """
    b = tuple(ref.bbox) if ref.bbox else ()
    if len(b) < 4:
        return 0.0
    w = min(b[2], bbox[2]) - max(b[0], bbox[0])
    h = min(b[3], bbox[3]) - max(b[1], bbox[1])
    full = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    if w <= 0 or h <= 0 or full <= 0:
        return 0.0
    return max(0.0, w * h) / full


def _rank_by_geometric_coverage(
    refs: Sequence[gstac.SceneRef], bbox: Sequence[float]
) -> list[gstac.SceneRef]:
    """按几何覆盖比降序排，覆盖相同时云量低的在前。

    先比覆盖再比云量，与 ``build_series`` 一贯的 ``(valid_px, area_km2)``
    口径一致：瓦片接缝处的窄条云量再低也没有湖。
    """
    return sorted(
        refs,
        key=lambda r: (-_geometric_coverage(r, bbox), r.cloud_cover),
    )


def _is_rate_limited(exc: BaseException) -> bool:
    """判断异常是不是服务端限流（HTTP 429）或连接被重置。

    429 和 connection reset 是同一类现象：请求发太密。它们**可以重试**，
    而「缺资产 / 越界」不能。混为一谈会让整年序列出现空洞。
    """
    text = f"{type(exc).__name__} {exc}"
    return "429" in text or "Too Many Requests" in text or "Connection was reset" in text


def _measure(
    ref: gstac.SceneRef,
    bbox: Sequence[float],
    ref_resolution: float | None = None,
    *,
    retries: int = 2,
) -> SeriesPoint:
    """读一景、算一期面积。失败时抛异常，由调用方决定是否跳过。

    遇到 429 / 连接重置会退避重试：这类失败是**瞬时的**，直接跳过会在
    序列里留一个假空洞（看起来像"那天没观测"，实际是没读成）。
    """
    last: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return _measure_once(ref, bbox, ref_resolution)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_rate_limited(exc) or attempt == retries:
                raise
            # 1 s、3 s 递增退避。限流窗口通常就是几秒，退太久反而拖慢整条序列。
            time.sleep(1.0 + 2.0 * attempt)
    raise last  # pragma: no cover - 循环内必然 return 或 raise


def _measure_once(
    ref: gstac.SceneRef,
    bbox: Sequence[float],
    ref_resolution: float | None = None,
) -> SeriesPoint:
    """读一景、算一期面积（不做重试）。"""
    sources, headers = gstac.sign_scene(ref, bands=SERIES_BANDS)
    # HTTP/2 + 多路复用：同一景的 3 个波段共用一条 TCP 连接，把三次
    # TLS 握手摊成一次。实测 12 个日期并发 10 路时 14.34 s → 11.41 s（1.26x）。
    # 这是零风险改动（协议层优化，不改任何数值口径）。
    with rasterio.Env(**_HTTP_ENV):
        scene = gio.load_scene(
            sources, S2_L2A_6BAND, bounds=bbox,
            ref_resolution=ref_resolution, http_headers=headers or None)

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
    ref_resolution: float | None = None,
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
    ref_resolution
        读取分辨率（米）。**None = 10 m 原生，这是唯一可用于正式结果的口径。**
        传 20 只适合快速预览：实测（2025 年 8 个日期）20 m 与 10 m 的
        面积相对差中位 11.6%、最大 30.5%。面积是风险判据的核心输入
        （冰坝湖看面积/平台比值），30% 的偏差足以翻转等级，所以
        **不要把 20 m 的结果写进报告或与历史序列比较**。
        它省的是下行流量（窗口像元数降到 1/4），实测 16 个日期
        从约 24 s 降到约 12 s。

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
    total = len(years_list := list(years))

    for year_idx, year in enumerate(years_list, start=1):
        # 每年开头报一次：检索本身要 2 秒，年份多的时候光检索就很久。
        emit(f"[{year_idx}/{total}] {year} 检索中 …")
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

        dates = sorted(by_date)
        # 每天可能有 2 景（相邻瓦片，AOI 压在接缝上）。实测 2025 年
        # 16 个日期里 12 个是 2 景，总共 28 景——原来逐景读再比较，
        # 等于把请求数翻了一倍。
        #
        # STAC 的 item bbox 就能算出覆盖比（实测 T43SFV 1.00 vs
        # T43SGV 0.42），不必读像素就能挑出覆盖最完整的那景。**只读它**。
        # 保留"读第二景"的退路：若第一景读失败或实际覆盖不足，才回退。
        n_multi = sum(1 for v in by_date.values() if len(v) > 1)
        emit(f"[{year_idx}/{total}] {year} 共 {len(dates)} 个日期"
             f"（{sum(len(v) for v in by_date.values())} 景"
             f"{f'，{n_multi} 天有双瓦片，按几何预筛只读主景' if n_multi else ''}），开始读取")

        # ---- 整年并发读取 ----
        #
        # 实测（scripts/bench_network.py，2026-11-02）：串行 4 景 36.38 s，
        # 并发 6 路 5.81 s（加速 6.26x）。瓶颈不是下行带宽，而是**每次
        # HTTP 请求的固定开销**——TLS 握手 + 首字节延迟约 2.5 s，而一个
        # 512×512 块只有 0.5 MB。串行时这段延迟完全暴露，并发才能把它盖住。
        #
        # 为什么是「整年」而不是「每景内部并发」：load_scene 内部已经把
        # 同景的多个波段并发读了（io.py），但那只是 3–5 个请求；一整年
        # 有 16–25 个日期，把日期铺开才能把并发度做满。
        #
        # 并发度取 min(日期数, 4)。
        #
        # 这不是拍脑袋，是被服务端打回来的：实测并发 10 路时 Planetary
        # Computer 直接回 **HTTP 429 Too Many Requests**，整年 16 个日期里
        # 有 8 个读失败（「全部读取失败」）。429 是限流，不是网络抖动——
        # 同一个 SAS token 下请求太密。4 路是多次复测里既不触发 429、
        # 又明显快于串行的值。
        #
        # 注意：每个日期内部，load_scene 还会对 3 个波段并发（io.py），
        # 所以实际并发请求数是 4 × 3 = 12 条。再往上就进 429 区间了。
        max_workers = min(len(dates), 4)
        done = 0
        lock = threading.Lock()

        def measure_date(date: str):
            """读一个日期：先按几何挑主景，读不到再回退到其余景。"""
            nonlocal done
            candidates = _rank_by_geometric_coverage(by_date[date], bbox)
            best: SeriesPoint | None = None
            for ref in candidates:
                try:
                    point = _measure(ref, bbox, ref_resolution)
                except Exception:  # noqa: BLE001 - 单景读失败跳过
                    continue
                # 先比有效覆盖，再比面积：覆盖不足的景面积再大也是假的。
                # 几何预筛只是省请求，最终仍以实际读到的有效像元为准——
                # STAC bbox 是粗粒度包络，实际瓦片可能因云/条带更窄。
                if best is None or (point.valid_px, point.area_km2) > (
                    best.valid_px,
                    best.area_km2,
                ):
                    best = point
                # 已读到覆盖 95% 以上的景就不必再试别的了：实测完整瓦片
                # 的 valid_px 是 378324，接缝窄条只有 7 万，差一个量级。
                if point.valid_frac >= 0.95:
                    break
            # 进度在完成时回调（不是开始前）：并发下"开始"的先后没有意义，
            # 而且 cancel() 靠这个回调点中断，回调太早会让已完成的工作白费。
            with lock:
                done += 1
                emit(f"[{year_idx}/{total}] {year} {date} "
                     f"({done}/{len(dates)}) 已读取")
            return best

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(measure_date, dates))

        for date, best in zip(dates, results):
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
