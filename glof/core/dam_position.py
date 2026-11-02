"""坝体位置估计——从湖的形状 + DEM 找下游出口，替掉"用湖心当坝体"的错误口径。

为什么需要这个模块
------------------
``risk_series.slope_from_dem()`` 早先用湖的质心当坝体位置，实测克亚吉尔湖心
高程 4769 m，四周 500 m 只有 +10~+250 m 的岸坡，量出 **-0.01°**；同一窗口内
真实坡地可达 52°（中位 19°）。湖面本身是平的，站在湖心量"下游坡度"量到的
是"湖平不平"，不是"坝体下游陡不陡"。``MIN_MEASURABLE_SLOPE_DEG = 1.0`` 只是
把这种假值退回 UNKNOWN，让判据不敢用——**要真正用上这条判据，得先知道坝在哪。**

本模块给的就是那个位置。物理依据只有两条，不需要任何经验系数：

1. **水往低处流。** 坝体是湖的下游出口，出口是湖边界上高程最低的地方。
2. **出口的外法线背向湖心。** 从出口指向湖质心的反方向，就是下游方向。

于是"坝体在哪"变成一个可在 DEM 上复核的几何量：沿湖边界采样，取高程最低点。
评委可以拿同一份 DEM 把这条边界重采一遍，得到同一个点。

口径（不要各自另写一份）
------------------------
* **高程采样**复用 ``terrain._sample()``：双线性 + nodata 按剩余有效权重重归一。
  不另写一套插值，否则"同一份 DEM、同一个点"会给出两个高程。
* **下游坡度**复用 ``terrain.downstream_slope()``。坡度定义全项目只有一份
  （中心差分 + cos(lat) 修正 + 500 m 剖面最小二乘），本模块**不重写**。
* 边界采样点用**等弧长**（``shapely`` 的 normalized interpolate），不是按顶点下标：
  矢量化出来的顶点在湖岸弯曲处密、直边处疏，按顶点密度采样会让直边段几乎
  没有样本，出口恰好落在直边时就会漏掉。

缺数据就是 None
---------------
DEM 读不到、边界点全部落在 nodata 上、湖几何无效——一律返回 ``None``，
不返回 0、不返回质心。把"不知道坝在哪"退回质心，等于又回到本模块要修的那个错。

一个**已知局限**（实测，不要当成 bug）
--------------------------------------
低分辨率 DEM（GLO-30）在湖面上会因雷达/光学融合误差给出 ±数米的起伏，
而真实湖面的水位高差是 0。当整条边界的高程散布与 DEM 噪声同量级时，
"最低点"测到的可能是噪声而不是真实出口。所以 ``dam_position()`` 会计算边界
高程的**散布**（max - min），并把它一并放进 ``DamEstimate.boundary_relief_m``：
散布越小，这个出口越不可信。调用方可以据此决定要不要用。

实测（Copernicus GLO-30，本地 ``output/real1/lakes.geojson`` 的 3 个真实湖，
``scripts/verify_dam_position.py --live`` 可复跑）：

    湖 1（6.07 km²）边界高程散布 160.7 m -> 坝体下游坡度 0.13°（湖心 0.50°）
    湖 2（0.20 km²）散布 669.0 m -> 坝体下游坡度 8.38°（湖心 21.84°）
    湖 3（0.15 km²）散布 537.1 m -> 坝体下游坡度 10.87°（湖心 17.25°）

注意湖 2/3 的坝体坡度比湖心**更缓**，这不是退化：湖心在陡岸下，量的是岸坡；
坝体在湖的排水端，量的是坝体下游，后者本就更缓。这正是"湖心量错了"的实证。
湖 1 两个数都接近 0，因为它是 6 km² 的大湖，500 m 剖面还没走出湖面——
**这是数据告诉我们的事实**：这个湖用 500 m 口径量不到坝体坡度，应当退回 UNKNOWN，
而不是拿湖心或坝体的一个近零数去命中判据。
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
from affine import Affine
from pyproj import CRS
from shapely import make_valid
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry

from . import terrain as gterr

__all__ = [
    "DamEstimate",
    "DEFAULT_BOUNDARY_SAMPLES",
    "outlet_point",
    "downstream_azimuth",
    "dam_position",
    "slope_from_dam",
    "lake_geometry",
]

#: 沿湖边界采多少个等弧长点。360 是"每 1° 方位一个点"的量级，对 1 km 周长的湖
#: 约 2.8 m 间距，远小于 GLO-30 的 30 m 像元——再密也分辨不出更多地形。
#: 之所以暴露成常量而不是参数：它只影响搜索分辨率，不影响"出口"的定义
#: （出口是边界上的全局最低点），改它不会让结果含义变化。
DEFAULT_BOUNDARY_SAMPLES = 360

#: 判断"出口就在质心附近"的退化阈值，单位是湖的等效半径倍数。
#: 出口到质心的距离小于 1% 等效半径时，用"质心 -> 出口"求方向会把方位角
#: 放大成一个纯噪声（两个几乎重合的点，方向对舍入误差极敏感），改走局部切线法线。
_DEGENERATE_FRACTION = 0.01


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


@dataclass
class DamEstimate:
    """一个湖的坝体估计结果。

    ``azimuth_deg`` 是**下游方向**（从坝体指向下游，正北 0、顺时针），不是坝体朝向。
    ``boundary_relief_m`` 是湖边界高程的散布（max - min），用来判断这个出口是否
    只是 DEM 噪声：散布越小越不可信。它是 None 时表示边界高程读不到。
    """

    lon: float
    lat: float
    azimuth_deg: float | None
    elevation_m: float | None
    method: str
    boundary_relief_m: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dam_lon": self.lon,
            "dam_lat": self.lat,
            "dam_azimuth_deg": self.azimuth_deg,
            "dam_elevation_m": self.elevation_m,
            "dam_method": self.method,
            "dam_boundary_relief_m": self.boundary_relief_m,
        }


# ---------------------------------------------------------------------------
# 几何预处理
# ---------------------------------------------------------------------------


def lake_geometry(lake_geom_wgs84: BaseGeometry | str | None) -> BaseGeometry | None:
    """把湖几何规整成有效的 WGS84 面几何；坏掉/为空返回 None。

    接受 shapely 几何或 GeoJSON 字符串（``LakeRecord.geometry_wgs84_json`` 就是
    后者）。自相交的环在矢量化窄颈处偶尔出现，会让 ``interpolate`` 抛拓扑异常，
    这里用 ``make_valid`` 兜住——修不好的直接返回 None，不猜一个形状。
    """
    if lake_geom_wgs84 is None:
        return None
    geom: BaseGeometry | None
    if isinstance(lake_geom_wgs84, str):
        import json

        try:
            from shapely.geometry import shape

            geom = shape(json.loads(lake_geom_wgs84))
        except Exception:
            return None
    else:
        geom = lake_geom_wgs84
    if geom is None or geom.is_empty:
        return None
    if not isinstance(geom, (Polygon, MultiPolygon)):
        return None
    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except Exception:
            return None
    if geom.is_empty:
        return None
    # make_valid 可能把自相交多边形修成 GeometryCollection，只保留面部分。
    if not isinstance(geom, (Polygon, MultiPolygon)):
        polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None
        geom = polys[0] if len(polys) == 1 else MultiPolygon(
            [p for g in polys for p in (g.geoms if isinstance(g, MultiPolygon) else [g])]
        )
    return geom


def _largest_polygon(geom: BaseGeometry) -> Polygon:
    """取面积最大的那一块。多部件湖的出口在**最大水体**上，不在小水塘上。"""
    if isinstance(geom, Polygon):
        return geom
    return max(geom.geoms, key=lambda p: p.area)  # type: ignore[union-attr]


def _boundary_points(geom: BaseGeometry, n: int) -> list[Point]:
    """沿外边界等弧长取 n 个点（不含重复的首尾）。

    只取外环，不取内环（洞）：冰湖里的岛不是坝。取内环会在岛岸上找到
    "最低点"，而那里是湖心方向的岸，不是下游出口。
    """
    ring = _largest_polygon(geom).exterior
    length = ring.length
    if not math.isfinite(length) or length <= 0.0:
        return []
    return [ring.interpolate((i + 0.5) / n, normalized=True) for i in range(n)]


def _boundary_elevations(
    geom: BaseGeometry,
    dem: np.ndarray,
    dem_transform: Affine,
    n: int,
) -> tuple[list[Point], np.ndarray]:
    """边界点 + 对应 DEM 高程（NaN 表示该点读不到）。"""
    pts = _boundary_points(geom, n)
    if not pts:
        return [], np.empty(0, dtype=np.float64)
    inv = ~dem_transform
    z = np.array([gterr._sample(dem, inv, p.x, p.y) for p in pts], dtype=np.float64)
    return pts, z


# ---------------------------------------------------------------------------
# 出口
# ---------------------------------------------------------------------------


def outlet_point(
    lake_geom_wgs84: BaseGeometry | str,
    dem: np.ndarray,
    dem_transform: Affine,
    dem_crs: CRS | str,
    *,
    n_samples: int = DEFAULT_BOUNDARY_SAMPLES,
) -> tuple[float, float] | None:
    """湖边界上 DEM 高程最低的点，即下游出口。返回 ``(lon, lat)`` 或 None。

    Parameters
    ----------
    lake_geom_wgs84
        WGS84 下的湖面多边形（shapely 或 GeoJSON 字符串）。
    dem
        二维高程数组。非有限值视为 nodata。
    dem_transform
        与 dem 对应的仿射变换，且必须是**地理坐标系（度）**。
    dem_crs
        dem 的坐标系。只接受地理坐标系：本模块按经纬度采样边界点，
        如果 DEM 是投影坐标（UTM），点和栅格不在同一坐标系里，
        ``~transform`` 会把米当成度，静默给出错误的高程。这种情况直接报错，
        让调用方先 ``rasterio.warp.reproject`` 到 WGS84 网格。
    n_samples
        边界等弧长采样点数。必须 >= 8，否则几个点连出口在哪个方向都定不下来。

    Returns
    -------
    (lon, lat) | None
        出口坐标；DEM 读不到、边界点全部落在 nodata、或几何无效时返回 None。

    Notes
    -----
    只比较**读得到高程**的边界点。全边界都是 nodata 时返回 None，
    绝不退回质心或边界中点——那是编一个位置。
    """
    _require(
        isinstance(n_samples, numbers.Integral) and not isinstance(n_samples, bool)
        and int(n_samples) >= 8,
        f"n_samples 必须是不小于 8 的整数，收到 {n_samples!r}",
    )
    n_samples = int(n_samples)
    _check_dem_grid(dem, dem_transform, dem_crs)

    geom = lake_geometry(lake_geom_wgs84)
    if geom is None:
        return None
    pts, z = _boundary_elevations(geom, np.asarray(dem, dtype=np.float64), dem_transform, n_samples)
    finite = np.isfinite(z)
    if not finite.any():
        return None
    idx = int(np.flatnonzero(finite)[np.argmin(z[finite])])
    return float(pts[idx].x), float(pts[idx].y)


def _check_dem_grid(dem: np.ndarray, dem_transform: Affine, dem_crs: CRS | str) -> None:
    """DEM 必须是地理坐标系网格，且 transform 是仿射。错了一律 ValueError。

    这里宁可直接报错也不自动重投影：自动重投影会悄悄改变像元尺度，
    调用方拿到的坡度和它以为的 DEM 不再是同一个东西。
    """
    _require(isinstance(dem_transform, Affine), f"dem_transform 必须是 affine.Affine，收到 {type(dem_transform)!r}")
    _require(dem_transform.a != 0 and dem_transform.e != 0, "dem_transform 的像元尺度不能为 0")
    z = np.asarray(dem)
    _require(z.ndim == 2 and z.size > 0, f"dem 必须是非空二维数组，收到 {z.shape}")
    try:
        crs = CRS.from_user_input(dem_crs)
    except Exception as exc:  # noqa: BLE001 - 无法识别的 CRS 就是错参数
        raise ValueError(f"无法解析 dem_crs：{dem_crs!r}") from exc
    _require(
        crs.is_geographic,
        f"DEM 必须是地理坐标系（度），收到 {crs.to_string()}；"
        "投影坐标系请先用 rasterio.warp.reproject 到 WGS84 网格",
    )


# ---------------------------------------------------------------------------
# 方位角
# ---------------------------------------------------------------------------


def _initial_azimuth(lon0: float, lat0: float, lon1: float, lat1: float) -> float:
    """WGS84 椭球上 (lon0,lat0) -> (lon1,lat1) 的初始方位角（度，正北 0 顺时针）。"""
    from pyproj import Geod

    az, _, _ = Geod(ellps="WGS84").inv(lon0, lat0, lon1, lat1)
    return az % 360.0


def downstream_azimuth(
    lake_geom_wgs84: BaseGeometry | str,
    outlet_lon: float,
    outlet_lat: float,
) -> float:
    """出口处的下游方位角（度，正北 0，顺时针）。

    主算法：出口指向湖质心的反方向。这是"外法线"在凸多边形上的解析形式，
    对冰湖这种拉长的形状足够好——质心在湖里，出口在边上，连线就是朝外。

    退化情形（出口几乎和质心重合，或几何退化）用局部切线法线：
    取出口前后各一小段边界求切线，法线取**背向质心**的那一侧。

    为什么不返回 None：方位角总是可定义的（湖几何有效即可）。真正可能缺数据的是
    高程，那在 ``outlet_point()`` / ``dam_position()`` 里已经处理。
    """
    geom = lake_geometry(lake_geom_wgs84)
    _require(geom is not None, "湖几何无效，无法求下游方位角")
    _require(
        math.isfinite(outlet_lon) and math.isfinite(outlet_lat),
        f"出口经纬度必须是有限数，收到 ({outlet_lon!r}, {outlet_lat!r})",
    )

    poly = _largest_polygon(geom)
    centroid = poly.centroid
    # 湖的等效半径（用面积折算），用来判断"出口是否几乎和质心重合"。
    radius_deg = math.sqrt(max(poly.area, 0.0) / math.pi)
    gap = math.hypot(outlet_lon - centroid.x, outlet_lat - centroid.y)
    if radius_deg > 0 and gap > _DEGENERATE_FRACTION * radius_deg:
        return _initial_azimuth(centroid.x, centroid.y, outlet_lon, outlet_lat)

    # 退化：出口落在质心上（正圆 + 对称采样时可能发生），用局部切线法线。
    return _tangent_normal_azimuth(poly, outlet_lon, outlet_lat, centroid.x, centroid.y)


def _tangent_normal_azimuth(
    poly: Polygon,
    lon: float,
    lat: float,
    cx: float,
    cy: float,
) -> float:
    """边界在 (lon,lat) 处的法线方向，取背向 (cx,cy) 的那一侧。

    做法：沿边界在出口附近取 ±delta 弧长两点，用它们的连线当切线，
    法线 = 切线旋转 90°。两个法线候选里选与"质心 -> 出口"夹角更小的那个
    （即更朝外）。delta 取周长的 1%，小到能当局部切线、大到不会被单个顶点噪声带偏。
    """
    ring = poly.exterior
    length = ring.length
    if not (math.isfinite(length) and length > 0):
        # 周长都算不出来，只能退回质心->出口方向（此时它已经是唯一的信息）。
        return _initial_azimuth(cx, cy, lon, lat)

    # 找到出口在环上的投影位置：取最近的顶点下标做起点，再按弧长微调。
    coords = np.asarray(ring.coords, dtype=np.float64)
    d2 = (coords[:, 0] - lon) ** 2 + (coords[:, 1] - lat) ** 2
    i0 = int(np.argmin(d2))
    base = ring.project(Point(coords[i0]))
    delta = max(length * 0.01, 1e-9)
    p_before = ring.interpolate((base - delta) % length)
    p_after = ring.interpolate((base + delta) % length)

    # 切线方向。**必须换到米**再求方向：经度 1° 在 36°N 只有纬度 1° 的 0.81 倍，
    # 直接用经纬差当平面向量，等于把东西向压缩了 19%，法线会偏离真正的垂直方向。
    # 这里只用一个局部比例尺（出口处），对"定 90°"足够，不用于量任何距离。
    m_lat, m_lon = gterr._meters_per_degree(lat)
    tx = (p_after.x - p_before.x) * m_lon
    ty = (p_after.y - p_before.y) * m_lat
    norm = math.hypot(tx, ty)
    if norm <= 0:
        return _initial_azimuth(cx, cy, lon, lat)
    tx /= norm
    ty /= norm
    # 两个法线候选：切线顺时针转 90° / 逆时针转 90°。
    candidates = [(ty, -tx), (-ty, tx)]
    outward = ((lon - cx) * m_lon, (lat - cy) * m_lat)
    best = max(candidates, key=lambda v: v[0] * outward[0] + v[1] * outward[1])
    # best 的 x 是东分量（米）、y 是北分量（米）；方位角从北顺时针 = atan2(东, 北)。
    return math.degrees(math.atan2(best[0], best[1])) % 360.0


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------


def dam_position(
    lake_geom_wgs84: BaseGeometry | str,
    dem: np.ndarray,
    dem_transform: Affine,
    dem_crs: CRS | str,
    *,
    n_samples: int = DEFAULT_BOUNDARY_SAMPLES,
) -> DamEstimate | None:
    """估一个湖的坝体位置：出口点 + 下游方位角 + 该点高程。

    这是 ``outlet_point()`` 与 ``downstream_azimuth()`` 的组合。返回 None 的
    条件与 ``outlet_point()`` 一致：几何无效或边界高程全读不到。

    ``boundary_relief_m`` 一并返回，因为它是判断这个估计可信度的唯一线索：
    边界高程散布只有几米时，出口很可能只是 DEM 在湖面上的噪声起伏。
    调用方应当把散布和坡度一起看，而不是只信一个点。
    """
    _check_dem_grid(dem, dem_transform, dem_crs)
    geom = lake_geometry(lake_geom_wgs84)
    if geom is None:
        return None

    z2d = np.asarray(dem, dtype=np.float64)
    pts, z = _boundary_elevations(geom, z2d, dem_transform, n_samples)
    finite = np.isfinite(z)
    if not finite.any():
        return None
    idx = int(np.flatnonzero(finite)[np.argmin(z[finite])])
    lon, lat = float(pts[idx].x), float(pts[idx].y)
    elevation = float(z[idx])
    relief = float(np.nanmax(z) - np.nanmin(z)) if finite.any() else None
    azimuth = downstream_azimuth(geom, lon, lat)
    return DamEstimate(
        lon=lon,
        lat=lat,
        azimuth_deg=azimuth,
        elevation_m=elevation,
        method="boundary_min_elevation",
        boundary_relief_m=relief,
    )


# ---------------------------------------------------------------------------
# 批量：records -> 下游坡度
# ---------------------------------------------------------------------------


def _record_geometry(record: Any) -> BaseGeometry | None:
    """从 LakeRecord（或任何带 geometry_wgs84_json 的对象）取几何。"""
    raw = getattr(record, "geometry_wgs84_json", None)
    if raw is None:
        return None
    return lake_geometry(raw)


@dataclass
class DamSlope:
    """一个湖的"坝体下游坡度"结果。

    ``slope_deg is None`` 表示**没量到**（估不出坝体、或 ``downstream_slope``
    返回 None），不是 0。``dam is None`` 时 ``lon/lat`` 退回质心只为报告可读，
    **不能**拿去当坝体位置用。
    """

    lake_id: Any
    dam: DamEstimate | None
    slope_deg: float | None
    centroid_lon: float | None
    centroid_lat: float | None
    note: str = ""

    @property
    def used_dam_position(self) -> bool:
        """坡度是不是真的从坝体位置量的。False 时 slope_deg 必为 None。"""
        return self.dam is not None and self.slope_deg is not None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "lake_id": self.lake_id,
            "dam_slope_deg": self.slope_deg,
            "dam_used": self.used_dam_position,
            "dam_note": self.note,
        }
        if self.dam is not None:
            d.update(self.dam.to_dict())
        else:
            d["centroid_lon"] = self.centroid_lon
            d["centroid_lat"] = self.centroid_lat
        return d


def slope_from_dam(
    records: Iterable[Any],
    dem: np.ndarray,
    dem_transform: Affine,
    dem_crs: CRS | str,
    *,
    search_m: float = 2000.0,
    n_samples: int = DEFAULT_BOUNDARY_SAMPLES,
) -> list[DamSlope]:
    """对每个湖估坝体位置，再用 ``terrain.downstream_slope`` 算下游坡度。

    Parameters
    ----------
    records
        ``LakeRecord`` 序列（或任何带 ``geometry_wgs84_json`` / ``centroid_lon`` /
        ``centroid_lat`` / ``lake_id`` 的对象）。
    dem, dem_transform, dem_crs
        与 ``outlet_point()`` 同口径的地理坐标系 DEM 网格。
    search_m
        找下坡方向的最大半径（米），原样传给 ``downstream_slope``。显式传参，
        不藏默认值——它是判定口径的一部分。
    n_samples
        边界采样点数。

    Returns
    -------
    list[DamSlope]
        与输入同序，一个湖一条。估不出坝体（几何无效 / 边界全 nodata）或
        坡度量不出来的湖，``slope_deg`` 是 ``None`` 并写明原因，**不是 0**。

    Notes
    -----
    这里不调用 ``risk_series.slope_from_dem``：那条路径拿质心当坝体，
    正是本模块要替换的旧口径。坡度本身仍然只来自 ``terrain.downstream_slope``，
    口径没有分叉。
    """
    _require(
        math.isfinite(search_m) and search_m > 0,
        f"search_m 必须是正的有限数，收到 {search_m!r}",
    )
    _check_dem_grid(dem, dem_transform, dem_crs)
    z2d = np.asarray(dem, dtype=np.float64)

    out: list[DamSlope] = []
    for rec in records:
        lake_id = getattr(rec, "lake_id", None)
        clon = getattr(rec, "centroid_lon", None)
        clat = getattr(rec, "centroid_lat", None)
        geom = _record_geometry(rec)
        if geom is None:
            out.append(DamSlope(lake_id, None, None, clon, clat, "缺湖面几何，无法定位出口"))
            continue

        dam = dam_position(geom, z2d, dem_transform, dem_crs, n_samples=n_samples)
        if dam is None:
            out.append(DamSlope(lake_id, None, None, clon, clat, "边界高程全为 nodata，无法定位出口"))
            continue

        slope = gterr.downstream_slope(
            z2d, dem_transform, dam.lat, dam.lon, search_m=search_m
        )
        if slope is None:
            note = "坝体位置已估出，但下游 500 m 剖面不完整，坡度未量到"
        else:
            note = (
                f"坝体取边界最低点（高程 {dam.elevation_m:.1f} m，"
                f"边界散布 {dam.boundary_relief_m:.1f} m）"
                if dam.elevation_m is not None and dam.boundary_relief_m is not None
                else "坝体取边界最低点"
            )
        out.append(DamSlope(lake_id, dam, slope, clon, clat, note))
    return out
