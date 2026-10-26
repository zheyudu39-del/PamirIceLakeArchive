"""水体提取、矢量化与面积统计——本项目的核心闭环。

流程（每一步都可以单独关掉，方便做消融实验）：

    NDWI 阈值 -> 排除掩膜 -> 形态学闭运算 -> 填洞 -> 连通域标记
    -> 最小面积过滤 -> 矢量化 -> 面积/周长/质心 -> 记录

两条容易踩的坑，代码里已经处理：

    1. 面积必须在**投影坐标系**里算。在经纬度上直接 .area 得到的是"平方度"，
       高原地区量级完全错。地理坐标系输入会按质心自动选 UTM 带。
    2. 像元面积和变换矩阵有关，带旋转的仿射变换不能简单用 a*e。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
from pyproj import CRS, Transformer
from rasterio.features import shapes as _raster_shapes
from rasterio.transform import Affine
from scipy import ndimage as ndi
from shapely.geometry import mapping, shape
from shapely.ops import transform as _shp_transform
from shapely.ops import unary_union
from skimage.filters import threshold_otsu
from skimage.measure import label as _label

__all__ = [
    "LakeRecord",
    "otsu_threshold",
    "otsu_threshold_surface",
    "extract_water_mask",
    "build_water_mask",
    "extract_lakes",
    "summarize",
]


@dataclass
class LakeRecord:
    """一个湖泊（一个连通水体）的提取结果。"""

    lake_id: int
    area_m2: float
    perimeter_m: float
    n_pixels: int
    area_px_m2: float
    mean_ndwi: float
    centroid_lon: float
    centroid_lat: float
    geometry_wgs84_json: str
    # 风险分级结果由 risk.py 填，这里只留字段：water.py 不该知道判据怎么算。
    # 默认值让老的构造调用（不传风险）继续可用。
    risk_level: str = "UNKNOWN"
    risk_reasons: list = field(default_factory=list)

    @property
    def area_km2(self) -> float:
        return self.area_m2 / 1e6

    @property
    def area_ha(self) -> float:
        return self.area_m2 / 1e4

    @property
    def area_check_ratio(self) -> float:
        """多边形面积 / 像元计数面积。接近 1 说明矢量化没有丢面。"""
        if self.area_px_m2 <= 0:
            return float("nan")
        return self.area_m2 / self.area_px_m2


def otsu_threshold(ndwi: np.ndarray, min_samples: int = 1000) -> float:
    """用 Otsu 自动选阈值。样本太少时不稳，直接报错让使用者显式给值。"""
    values = ndwi[np.isfinite(ndwi)]
    if values.size < min_samples:
        raise ValueError(
            f"有效像元只有 {values.size} 个，Otsu 阈值不可靠，请显式传 --threshold。"
        )
    return float(threshold_otsu(values))


def extract_water_mask(
    ndwi: np.ndarray,
    threshold: float | np.ndarray = 0.0,
    exclude: np.ndarray | None = None,
) -> np.ndarray:
    """纯阈值判水。NDWI > threshold 且像元有效，且不在排除掩膜内。

    threshold 可以是标量，也可以是逐像元阈值图（自适应阈值）。
    """
    water = np.isfinite(ndwi) & (ndwi > threshold)
    if exclude is not None:
        if exclude.shape != water.shape:
            raise ValueError(f"排除掩膜形状不一致：{exclude.shape} vs {water.shape}")
        water &= ~exclude
    return water


def build_water_mask(
    ndwi: np.ndarray,
    threshold: float | np.ndarray = 0.0,
    exclude: np.ndarray | None = None,
    *,
    nir: np.ndarray | None = None,
    nir_max: float | None = None,
    close_radius_px: int = 0,
    fill_holes: bool = True,
) -> np.ndarray:
    """阈值 + 光谱条件 + 排除掩膜 + 形态学处理，得到最终水体二值掩膜。

    Parameters
    ----------
    threshold
        标量，或 ``otsu_threshold_surface()`` 返回的逐像元阈值图。
    nir, nir_max
        **帕米尔冰湖的关键判据。** 实测这四个已知湖的 NIR 在 0.11–0.13，
        而雪/冰 0.6–0.9、植被与裸岩 0.25–0.45。加一条 ``NIR < 0.20``
        就能在低 NDWI 阈值下把雪缘和裸地全部挡掉，同时保住浑浊湖。
        不加这条，要么漏湖（阈值高），要么把雪缘当水（阈值低）。

    单独抽出来是为了让"落盘掩膜"和"统计面积"走同一条代码路径——
    否则调试时看到的 mask.tif 和表格里的数字对不上，会浪费很多时间。
    """
    water = extract_water_mask(ndwi, threshold, exclude)

    if nir is not None and nir_max is not None:
        if nir.shape != water.shape:
            raise ValueError(f"NIR 形状与 NDWI 不一致：{nir.shape} vs {water.shape}")
        water &= np.isfinite(nir) & (nir < nir_max)
        if exclude is not None:
            water &= ~exclude

    if close_radius_px > 0:
        water = ndi.binary_closing(water, structure=np.ones((3, 3), bool), iterations=close_radius_px)
        if exclude is not None:
            water &= ~exclude

    if fill_holes:
        # 刻意**不**在填洞后重新应用 exclude：湖面浮冰会被 NDSI 掩膜剔成一个洞，
        # 而我们要统计的是"水体范围"而不是"无冰可见水面"。云同理——湖中央一块云
        # 不应让整湖面积凭空少一块。这个取舍写在方案里比藏起来好。
        water = ndi.binary_fill_holes(water)

    return water


def otsu_threshold_surface(
    ndwi: np.ndarray,
    window_px: int = 256,
    stride_px: int | None = None,
    *,
    min_valid: int = 2000,
    smooth_sigma: float = 2.0,
) -> np.ndarray:
    """滑窗 Otsu，返回逐像元的阈值图（不是单一常数）。

    动机：帕米尔冰湖的 NDWI 从 0.03（高浊度）到 0.6（清澈）都有，
    一个全图常数阈值必然顾此失彼。

    .. warning::
        **这个方法对"小目标"效果有限。** Otsu 假设直方图双峰，
        当一个 256×256 的窗口里水体只占 1% 时，双峰落在"岩石 vs 植被"上，
        而不是"陆地 vs 水"。也就是说它会把陆地劈成两半，而不是找出湖。
        实测见 `scripts/validate_sites.py`：4 个已知湖的召回率并不比固定阈值好。

        真正解决浑浊湖漏检的是光谱条件（NDWI 低但 NIR 也低），
        见 ``build_water_mask(nir=..., nir_max=...)``。

    Parameters
    ----------
    window_px
        滑动窗口边长（像元）。10 m 数据上 256 像元约 2.6 km。
    stride_px
        步长，默认窗口的一半。重叠部分取均值后再高斯平滑，避免块状接缝。
    min_valid
        窗口内有效像元下限，不足则跳过该窗口。
    """
    from scipy.ndimage import gaussian_filter
    from skimage.filters import threshold_otsu

    ndwi = np.asarray(ndwi, dtype=np.float32)
    h, w = ndwi.shape
    stride = stride_px or max(1, window_px // 2)

    acc = np.zeros((h, w), dtype=np.float32)
    cnt = np.zeros((h, w), dtype=np.float32)

    for r0 in range(0, h, stride):
        r1 = min(r0 + window_px, h)
        for c0 in range(0, w, stride):
            c1 = min(c0 + window_px, w)
            block = ndwi[r0:r1, c0:c1]
            values = block[np.isfinite(block)]
            if values.size < min_valid:
                continue
            try:
                thr = threshold_otsu(values)
            except Exception:  # 全常数窗口，Otsu 无解
                continue
            acc[r0:r1, c0:c1] += thr
            cnt[r0:r1, c0:c1] += 1

    if not cnt.any():
        raise ValueError(
            f"没有窗口满足 min_valid={min_valid}，无法生成自适应阈值。"
            f" 调小 window_px 或 min_valid。"
        )

    surface = np.where(cnt > 0, acc / np.maximum(cnt, 1.0), np.nan)
    if np.isnan(surface).any():
        finite = ndwi[np.isfinite(ndwi)]
        fallback = float(threshold_otsu(finite)) if finite.size else 0.0
        surface = np.where(np.isnan(surface), fallback, surface)

    if smooth_sigma > 0:
        surface = gaussian_filter(surface, smooth_sigma)
    return surface.astype(np.float32)


def _metric_crs(crs: CRS, lon: float, lat: float) -> CRS:
    """为经纬度坐标系挑一个等距的 UTM 带；投影坐标系原样返回。"""
    if not crs.is_geographic:
        return crs
    zone = int((lon + 180.0) // 6.0) + 1
    epsg = (32600 if lat >= 0.0 else 32700) + zone
    return CRS.from_epsg(epsg)


def extract_lakes(
    ndwi: np.ndarray,
    transform: Affine,
    crs: CRS | str | None,
    *,
    threshold: float | str = 0.0,
    min_area_km2: float = 0.01,
    exclude: np.ndarray | None = None,
    nir: np.ndarray | None = None,
    nir_max: float | None = None,
    otsu_window_px: int = 256,
    close_radius_px: int = 0,
    fill_holes: bool = True,
    connectivity: int = 2,
) -> list[LakeRecord]:
    """从 NDWI 栅格提取湖泊并统计面积。

    Parameters
    ----------
    threshold
        数值、``"otsu"``（全图自动）或 ``"adaptive"``（滑窗 Otsu，见
        ``otsu_threshold_surface()`` 的警告）。
    nir, nir_max
        可选的 NIR 光谱上限。帕米尔冰湖靠这一条才能和雪缘分开，强烈建议传。
    min_area_km2
        最小面积过滤，默认 0.01 km²（=1 公顷），与 ICIMOD 编目口径接近。
    exclude
        排除掩膜（雪、云、阴影等），True 表示剔除。
    close_radius_px
        形态学闭运算的迭代次数。能补上湖面浮冰造成的窄缝，但会轻微膨胀面积，
        做精度评价时建议置 0 作为基线。
    fill_holes
        填掉完全被水包围的内部空洞（浮冰、小岛）。默认开——高原冰湖的浮冰
        很常见，不填会严重低估面积。注意填洞会把被掩膜剔除的浮冰/云洞一并补齐，
        因此统计口径是"水体范围"而不是"无冰可见水面"。
    """
    ndwi = np.asarray(ndwi, dtype=np.float32)
    if ndwi.ndim != 2:
        raise ValueError(f"NDWI 必须是二维数组，收到 {ndwi.shape}")
    if crs is None:
        raise ValueError("栅格缺少坐标系（CRS），无法计算面积。请先给影像定义投影。")
    crs = CRS.from_user_input(crs)

    if threshold == "otsu":
        threshold = otsu_threshold(ndwi)
    elif threshold == "adaptive":
        threshold = otsu_threshold_surface(ndwi, window_px=otsu_window_px)
    threshold = threshold if isinstance(threshold, np.ndarray) else float(threshold)

    water = build_water_mask(
        ndwi,
        threshold=threshold,
        exclude=exclude,
        nir=nir,
        nir_max=nir_max,
        close_radius_px=close_radius_px,
        fill_holes=fill_holes,
    )

    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    if pixel_area <= 0:
        raise ValueError("仿射变换退化，像元面积为 0，无法统计。")
    min_px = max(1, int(np.ceil(min_area_km2 * 1e6 / pixel_area)))

    lab = _label(water, connectivity=connectivity)
    if lab.max() == 0:
        return []

    counts = np.bincount(lab.ravel())
    keep = np.flatnonzero(counts >= min_px)
    keep = keep[keep != 0]
    if keep.size == 0:
        return []

    filtered = np.isin(lab, keep)

    parts: dict[int, list] = {}
    for geom_dict, value in _raster_shapes(
        lab.astype(np.int32), mask=filtered, transform=transform, connectivity=8
    ):
        parts.setdefault(int(value), []).append(shape(geom_dict))

    to_wgs84 = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)

    records: list[LakeRecord] = []
    for lid, geom_parts in parts.items():
        geom = unary_union(geom_parts)
        geom_wgs = _shp_transform(to_wgs84.transform, geom)
        centroid = geom_wgs.centroid

        metric = _metric_crs(crs, centroid.x, centroid.y)
        if metric == crs:
            geom_m = geom
        else:
            to_metric = Transformer.from_crs(crs, metric, always_xy=True)
            geom_m = _shp_transform(to_metric.transform, geom)

        in_lake = lab == lid
        values = ndwi[in_lake]
        values = values[np.isfinite(values)]

        records.append(
            LakeRecord(
                lake_id=0,
                area_m2=float(geom_m.area),
                perimeter_m=float(geom_m.length),
                n_pixels=int(counts[lid]),
                area_px_m2=float(counts[lid] * pixel_area),
                mean_ndwi=float(values.mean()) if values.size else float("nan"),
                centroid_lon=float(centroid.x),
                centroid_lat=float(centroid.y),
                geometry_wgs84_json=json.dumps(mapping(geom_wgs)),
            )
        )

    records.sort(key=lambda r: r.area_m2, reverse=True)
    for idx, rec in enumerate(records, start=1):
        rec.lake_id = idx
    return records


def summarize(records: list[LakeRecord]) -> dict[str, float | int]:
    """给 CLI 打印用的一句话汇总。"""
    return {
        "n_lakes": len(records),
        "total_area_km2": sum(r.area_km2 for r in records),
        "max_area_km2": max((r.area_km2 for r in records), default=0.0),
        "min_area_km2": min((r.area_km2 for r in records), default=0.0),
    }
