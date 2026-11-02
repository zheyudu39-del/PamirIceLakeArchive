"""暴露度：湖到最近道路 / 居民点的距离——GLOF 影响范围的代理指标。

为什么单独算暴露度
------------------
溃决风险不只取决于湖本身，还取决于洪水会淹到什么。同样是 HIGH 等级的湖，
紧邻 G314 国道和牧民定居点的那个，与方圆 30 km 无人烟的那个，处置优先级完全不同。
本模块把这件事拆成一个可复算的数：**湖心到最近道路 / 最近居民点的球面距离**。

这不是洪水演进模型。真正的淹没范围要 DEM + 溃口参数 + 水动力计算，
本模块只做筛查用的代理指标：距离越近，越值得优先派人去看。``buffer_km``
是一个可调的筛查口径，不是"洪水一定淹到 N km"的物理结论——所以它**没有默认值**，
调用方必须显式给。按 CLAUDE.md 的硬规矩，半径这类评价口径藏一个默认值就不可复现。

为什么不内置任何地理数据
------------------------
原计划从 OSM Overpass 抓 G314 与居民点。**实测不可达**（2026-11-02 复测）：

    * overpass-api.de/api/interpreter  -> HTTP 502「All query workers are busy」
    * overpass.kumi.systems            -> 连接超时（curl 28）

所以本模块**不带任何坐标**，只接受调用方传入的矢量文件。没给数据就返回 ``None``。
编一份"看起来合理"的 G314 坐标比缺数据危险得多：下游会把它当成量到的结果。

三态，不是两态
--------------
``settlement_distance_km`` / ``in_settlement_buffer`` 都有三种取值：

    True  量到了，且在范围内
    False 量到了，且不在范围内
    None  **没有数据，不知道**

没有居民点数据时写 ``False`` 会被读成"查过了，附近没人"——这是替用户断言了
一件没做过的事。全项目「缺数据返回 None」的约定在这里同样成立（见 CLAUDE.md）。

距离口径
--------
用 ``pyproj.Geod.inv`` 在 WGS84 椭球上算大地线距离，不用平面欧氏距离。
高原上这个差别是系统性的：36°N 处 1° 经度约 90.2 km，只有赤道 111.3 km 的 0.81 倍；
直接拿度数当距离，东西向会系统性偏大 23%。

点到线/面的距离不是只比顶点：一条道路的最近点通常落在两个顶点之间。
本模块把线段当大圆弧，用向量叉积求点到弧的最近点，再用 ``Geod.inv`` 量最终距离。
点落在多边形内部时距离为 0（湖在居民点面内，不是"到边界还有 1 km"）。

已实测的参考值（``scripts/verify_exposure.py`` 断言用）
--------------------------------------------------------
* 同经度 1° 纬差（36°N→37°N）：110.968 km
* 36°N 处 1° 经差（77°E→78°E）：90.163 km
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pyproj import Geod
from shapely.geometry import Point as _ShapelyPoint
from shapely.geometry import shape as _shapely_shape

__all__ = [
    "ExposureRecord",
    "load_features",
    "nearest_distance_km",
    "assess_exposure",
]

# WGS84 与项目所有影像、DEM 的基准一致。不提供换椭球的参数：
# 距离口径不该有隐藏开关，否则同一个湖换台机器算出不同的数。
_GEOD = Geod(ellps="WGS84")

_GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


# ---------------------------------------------------------------------------
# 读入
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_position(pos: Any, where: str) -> None:
    _require(
        isinstance(pos, (list, tuple)) and len(pos) >= 2,
        f"{where} 的坐标必须是 [经度, 纬度]（至少两个数），收到 {pos!r}",
    )
    lon, lat = pos[0], pos[1]
    _require(_is_number(lon), f"{where} 的经度不是有限数：{lon!r}")
    _require(_is_number(lat), f"{where} 的纬度不是有限数：{lat!r}")
    _require(-180.0 <= lon <= 180.0, f"{where} 的经度 {lon!r} 超出 [-180, 180]，可能经纬度写反了")
    _require(-90.0 <= lat <= 90.0, f"{where} 的纬度 {lat!r} 超出 [-90, 90]，可能经纬度写反了")


def _validate_coordinates(coords: Any, depth: int, where: str) -> None:
    """按 GeoJSON 的嵌套层数校验坐标。depth=0 表示这一层已经是 [lon, lat]。"""
    if depth == 0:
        _validate_position(coords, where)
        return
    _require(isinstance(coords, (list, tuple)), f"{where} 的 coordinates 应是数组，收到 {type(coords)!r}")
    for i, item in enumerate(coords):
        _validate_coordinates(item, depth - 1, f"{where}[{i}]")


# 每种几何类型的 coordinates 嵌套层数：Point 0，LineString 1，Polygon 2，
# MultiPolygon 3。MultiPoint/MultiLineString 分别比 Point/LineString 多一层。
_COORD_DEPTH = {
    "Point": 0,
    "MultiPoint": 1,
    "LineString": 1,
    "MultiLineString": 2,
    "Polygon": 2,
    "MultiPolygon": 3,
}


def _validate_geometry(geom: Any, where: str) -> None:
    _require(isinstance(geom, Mapping), f"{where} 的 geometry 必须是对象，收到 {type(geom)!r}")
    gtype = geom.get("type")
    _require(gtype in _GEOMETRY_TYPES, f"{where} 的几何类型 {gtype!r} 不受支持，可用：{sorted(_GEOMETRY_TYPES)}")
    if gtype == "GeometryCollection":
        members = geom.get("geometries")
        _require(isinstance(members, list), f"{where} 的 GeometryCollection 缺 geometries 数组")
        for i, member in enumerate(members):
            _validate_geometry(member, f"{where}.geometries[{i}]")
        return
    coords = geom.get("coordinates")
    _validate_coordinates(coords, _COORD_DEPTH[gtype], f"{where}.coordinates")


def _as_features(doc: Any) -> list[dict]:
    """把三种合法写法统一成 Feature 列表：FeatureCollection / Feature / 裸 geometry。"""
    _require(isinstance(doc, Mapping), "GeoJSON 顶层必须是对象")
    dtype = doc.get("type")
    if dtype == "FeatureCollection":
        features = doc.get("features")
        _require(isinstance(features, list), "FeatureCollection 的 features 必须是数组")
        out = []
        for i, item in enumerate(features):
            _require(isinstance(item, Mapping), f"features[{i}] 必须是对象")
            if item.get("type") == "Feature":
                out.append(item)
            elif item.get("type") in _GEOMETRY_TYPES:
                # 宽容处理：有人把裸几何直接塞进 features。归一成 Feature。
                out.append({"type": "Feature", "geometry": item, "properties": {}})
            else:
                raise ValueError(f"features[{i}] 既不是 Feature 也不是几何对象：{item.get('type')!r}")
        return out
    if dtype == "Feature":
        return [doc]
    if dtype in _GEOMETRY_TYPES:
        # "只含 Point/LineString/Polygon 的简单 GeoJSON"：顶层就是几何。
        return [{"type": "Feature", "geometry": doc, "properties": {}}]
    raise ValueError(f"无法识别的 GeoJSON 顶层类型 {dtype!r}")


def load_features(path: str | Path, *, layer: str | None = None) -> list[dict]:
    """读一个 GeoJSON 文件，返回 ``[{"geometry": ..., "properties": ...}, ...]``。

    支持 ``FeatureCollection``、单个 ``Feature``，以及顶层直接是
    ``Point`` / ``LineString`` / ``Polygon``（也含 Multi* 与 GeometryCollection）
    的简单 GeoJSON。每个要素的 geometry 与 properties 原样保留，不做坐标变换。

    文件不存在时抛 ``FileNotFoundError``——这是调用方给错了路径，不是"没数据"。
    真正的"没数据"是文件存在但要素为空，那种情况返回空列表，由
    ``nearest_distance_km()`` 转成 ``None``。两者不能混为一谈：
    路径写错要立刻报错，否则一次笔误会悄悄变成"研究区没有道路"。

    Parameters
    ----------
    path
        GeoJSON 路径（``.geojson`` / ``.json``）。
    layer
        预留给将来支持 GeoPackage 等带图层的格式。GeoJSON 没有图层概念，
        传非 None 会直接报错，而不是悄悄忽略——静默忽略参数比报错更难查。

    Returns
    -------
    list[dict]
        要素列表。geometry 为 ``None`` 的要素也保留（GeoJSON 允许），
        距离计算时跳过。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"矢量文件不存在：{p}。路径由调用方给出，请检查是否写错；"
            "本模块不内置任何地理数据"
        )
    if layer is not None:
        raise ValueError(
            f"GeoJSON 没有图层概念，layer={layer!r} 无法生效；"
            "需要多图层请先导出成单独的 GeoJSON 文件"
        )

    text = p.read_text(encoding="utf-8-sig")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} 不是合法 JSON：{exc}") from exc

    out: list[dict] = []
    for i, feature in enumerate(_as_features(doc)):
        geometry = feature.get("geometry")
        if geometry is not None:
            _validate_geometry(geometry, f"{p.name} 第 {i + 1} 个要素")
        properties = feature.get("properties") or {}
        _require(isinstance(properties, Mapping), f"{p.name} 第 {i + 1} 个要素的 properties 必须是对象")
        out.append({"geometry": geometry, "properties": dict(properties)})
    return out


# ---------------------------------------------------------------------------
# 球面几何
# ---------------------------------------------------------------------------


def _unit(lon: float, lat: float) -> tuple[float, float, float]:
    """经纬度 -> 单位球面上的笛卡尔坐标。"""
    lam = math.radians(lon)
    phi = math.radians(lat)
    cos_phi = math.cos(phi)
    return (cos_phi * math.cos(lam), cos_phi * math.sin(lam), math.sin(phi))


def _lonlat(v: tuple[float, float, float]) -> tuple[float, float]:
    lon = math.degrees(math.atan2(v[1], v[0]))
    lat = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
    return lon, lat


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _angle(a, b) -> float:
    """两个单位向量之间的夹角（弧度）。atan2 比 acos 在接近 0/π 时稳定。"""
    return math.atan2(_norm(_cross(a, b)), _dot(a, b))


def _geod_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """WGS84 椭球上的大地线距离（米）。"""
    return float(_GEOD.inv(lon1, lat1, lon2, lat2)[2])


def _point_distance_m(lon: float, lat: float, pos: Sequence[float]) -> float:
    return _geod_m(lon, lat, float(pos[0]), float(pos[1]))


def _segment_distance_m(lon: float, lat: float, a: Sequence[float], b: Sequence[float]) -> float:
    """点到线段（大圆弧）的最近距离（米）。

    只比两个端点是不够的：道路的最近点通常落在两个顶点之间。
    做法是把线段所在的大圆求出来，把查询点投影到该大圆上得到候选最近点，
    再判断候选点是否落在这段弧内；不在弧内就退回较近的端点。

    大圆与椭球大地线在几十公里尺度上的偏差远小于道路数据的顶点间距，
    最终距离仍用 ``Geod.inv`` 量，所以不会引入平面近似的系统性偏差。
    """
    va = _unit(float(a[0]), float(a[1]))
    vb = _unit(float(b[0]), float(b[1]))
    vp = _unit(lon, lat)

    d_a = _point_distance_m(lon, lat, a)
    d_b = _point_distance_m(lon, lat, b)

    normal = _cross(va, vb)
    n_norm = _norm(normal)
    if n_norm < 1e-12:
        # 两端点重合（或严格对跖），没有唯一的大圆，端点距离就是答案。
        return min(d_a, d_b)

    normal = (normal[0] / n_norm, normal[1] / n_norm, normal[2] / n_norm)
    along = _dot(vp, normal)
    proj = (vp[0] - along * normal[0], vp[1] - along * normal[1], vp[2] - along * normal[2])
    p_norm = _norm(proj)
    if p_norm < 1e-12:
        # 查询点正好在大圆的极点上：到弧上每一点都是 90°，端点即最近。
        return min(d_a, d_b)

    candidate = (proj[0] / p_norm, proj[1] / p_norm, proj[2] / p_norm)
    # 候选点落在弧 a→b 内，当且仅当 a→c 与 c→b 两段角距之和等于 a→b。
    # 容差按浮点误差给，不是业务阈值。
    if _angle(va, candidate) + _angle(candidate, vb) <= _angle(va, vb) + 1e-9:
        c_lon, c_lat = _lonlat(candidate)
        return _geod_m(lon, lat, c_lon, c_lat)
    return min(d_a, d_b)


def _line_distance_m(lon: float, lat: float, positions: Sequence[Sequence[float]]) -> float | None:
    if not positions:
        return None
    if len(positions) == 1:
        return _point_distance_m(lon, lat, positions[0])
    return min(
        _segment_distance_m(lon, lat, positions[i], positions[i + 1])
        for i in range(len(positions) - 1)
    )


def _polygon_contains(lon: float, lat: float, geom: Mapping[str, Any]) -> bool:
    """点是否落在面内（含边界）。面本身是拓扑关系，与投影无关。"""
    try:
        return bool(_shapely_shape(geom).covers(_ShapelyPoint(lon, lat)))
    except Exception:
        # 环没闭合之类的非法面：不做"内部"判断，退回只量到边界。
        # 宁可把面内的点算成到边界的距离（偏大），也不能因为一个坏面让整个评估崩掉。
        return False


def _distance_to_geometry_m(lon: float, lat: float, geom: Mapping[str, Any]) -> float | None:
    """点到单个几何要素的距离（米）。几何为空或不可用时返回 None。"""
    gtype = geom.get("type")

    if gtype == "GeometryCollection":
        values = [
            d
            for member in geom.get("geometries") or []
            if (d := _distance_to_geometry_m(lon, lat, member)) is not None
        ]
        return min(values) if values else None

    coords = geom.get("coordinates")
    if coords is None:
        return None

    if gtype == "Point":
        return _point_distance_m(lon, lat, coords)
    if gtype == "MultiPoint":
        return min((_point_distance_m(lon, lat, p) for p in coords), default=None)

    if gtype == "LineString":
        return _line_distance_m(lon, lat, coords)
    if gtype == "MultiLineString":
        values = [
            d for line in coords if (d := _line_distance_m(lon, lat, line)) is not None
        ]
        return min(values) if values else None

    if gtype in ("Polygon", "MultiPolygon"):
        if _polygon_contains(lon, lat, geom):
            return 0.0
        # 边界：外环 + 内环。点在洞里时，最近的边是洞的边，不是外环。
        rings = [coords] if gtype == "Polygon" else list(coords)
        values = [
            d
            for polygon in rings
            for ring in polygon
            if (d := _line_distance_m(lon, lat, ring)) is not None
        ]
        return min(values) if values else None

    return None


def _iter_feature_dicts(features: Any) -> Iterable[Mapping[str, Any]]:
    """接受 load_features 的返回值，也容忍直接传 FeatureCollection / 单个 Feature。"""
    if features is None:
        return []
    if isinstance(features, Mapping):
        if features.get("type") == "FeatureCollection":
            return [f for f in features.get("features") or [] if isinstance(f, Mapping)]
        return [features]
    return [f for f in features if isinstance(f, Mapping)]


def nearest_distance_km(lon: float, lat: float, features: Sequence[dict] | None) -> float | None:
    """点 (lon, lat) 到最近要素的球面距离（km）。

    ``features`` 为空（或所有要素都没有几何）时返回 ``None``——这是"没有数据"，
    不是"距离为零"。调用方不能用 ``or 0`` 把它兜底成 0。

    Parameters
    ----------
    lon, lat
        WGS84 经纬度（度）。
    features
        ``load_features()`` 返回的要素列表；也接受 FeatureCollection 或单个 Feature。

    Returns
    -------
    float | None
        最近距离（km，WGS84 大地线）。点在面要素内部时为 0.0。
    """
    _require(_is_number(lon) and _is_number(lat), f"经纬度必须是有限数，收到 ({lon!r}, {lat!r})")
    _require(-180.0 <= lon <= 180.0, f"经度 {lon!r} 超出 [-180, 180]")
    _require(-90.0 <= lat <= 90.0, f"纬度 {lat!r} 超出 [-90, 90]")

    best_m: float | None = None
    for feature in _iter_feature_dicts(features):
        geometry = feature.get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        distance = _distance_to_geometry_m(lon, lat, geometry)
        if distance is not None and (best_m is None or distance < best_m):
            best_m = distance
    return None if best_m is None else best_m / 1000.0


# ---------------------------------------------------------------------------
# 暴露度评估
# ---------------------------------------------------------------------------


@dataclass
class ExposureRecord:
    """一个湖的暴露度结果。

    三个距离/判定字段都可能是 ``None``，含义是**没有数据、不知道**：

    * ``road_distance_km is None``：没有道路数据，或湖心坐标不可用；
    * ``settlement_distance_km is None``：没有居民点数据，或湖心坐标不可用；
    * ``in_*_buffer is None``：对应的距离没量到，无法判断是否在范围内。

    ``False`` 只表示"量到了，且不在范围内"。两者不能互换。
    """

    lake_id: int
    road_distance_km: float | None = None
    settlement_distance_km: float | None = None
    in_road_buffer: bool | None = None
    in_settlement_buffer: bool | None = None

    def to_properties(self) -> dict[str, Any]:
        """可直接并进 GeoJSON properties 的扁平字典。"""
        return {
            "lake_id": self.lake_id,
            "exposure_road_distance_km": self.road_distance_km,
            "exposure_settlement_distance_km": self.settlement_distance_km,
            "exposure_in_road_buffer": self.in_road_buffer,
            "exposure_in_settlement_buffer": self.in_settlement_buffer,
        }

    def describe(self) -> str:
        """人读文本。None 明确写成"无数据"，不写成 0。"""
        def fmt(value: float | None) -> str:
            return "无数据" if value is None else f"{value:.3f} km"

        def flag(value: bool | None) -> str:
            return "未知" if value is None else ("是" if value else "否")

        return (
            f"湖 {self.lake_id}：最近道路 {fmt(self.road_distance_km)}"
            f"（缓冲区内 {flag(self.in_road_buffer)}），"
            f"最近居民点 {fmt(self.settlement_distance_km)}"
            f"（缓冲区内 {flag(self.in_settlement_buffer)}）"
        )


def _in_buffer(distance_km: float | None, buffer_km: float) -> bool | None:
    if distance_km is None:
        return None
    return distance_km <= buffer_km


def assess_exposure(
    records: Sequence[Any],
    *,
    roads_path: str | Path,
    settlements_path: str | Path | None = None,
    buffer_km: float,
) -> list[ExposureRecord]:
    """对每个湖算到最近道路 / 居民点的距离，并判断是否在影响范围内。

    道路是必给数据源（``roads_path`` 没有默认值）；居民点可选。没给居民点时，
    所有 ``settlement_distance_km`` 与 ``in_settlement_buffer`` 都是 ``None``，
    **不是 False**——"没查"和"查了没有"是两回事。

    距离 <= ``buffer_km`` 判为"在影响范围内"。``buffer_km`` 是筛查口径，
    **没有默认值**，必须由调用方按场景给——它是评价结论的一部分，藏起来就不可复现。

    Parameters
    ----------
    records
        ``water.LakeRecord`` 列表（或任何带 ``lake_id`` / ``centroid_lon`` /
        ``centroid_lat`` 属性的对象）。顺序与返回列表一致。
    roads_path
        道路矢量文件（GeoJSON）。
    settlements_path
        居民点矢量文件（GeoJSON）。``None`` 表示没有这个数据源。
    buffer_km
        影响范围半径（km），必须为正的有限数。

    Returns
    -------
    list[ExposureRecord]
        与 ``records`` 等长、同序。
    """
    _require(
        _is_number(buffer_km) and buffer_km > 0,
        f"buffer_km 必须是正的有限数，收到 {buffer_km!r}",
    )
    roads = load_features(roads_path)
    settlements = load_features(settlements_path) if settlements_path is not None else None

    out: list[ExposureRecord] = []
    for record in records:
        lake_id = getattr(record, "lake_id", None)
        lon = getattr(record, "centroid_lon", None)
        lat = getattr(record, "centroid_lat", None)
        valid = _is_number(lon) and _is_number(lat)

        road_km = nearest_distance_km(float(lon), float(lat), roads) if valid else None
        settlement_km = (
            nearest_distance_km(float(lon), float(lat), settlements)
            if valid and settlements is not None
            else None
        )

        out.append(
            ExposureRecord(
                lake_id=lake_id,
                road_distance_km=road_km,
                settlement_distance_km=settlement_km,
                in_road_buffer=_in_buffer(road_km, float(buffer_km)),
                in_settlement_buffer=_in_buffer(settlement_km, float(buffer_km)),
            )
        )
    return out
