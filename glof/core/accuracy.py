"""精度评价——点位召回、像元混淆矩阵、多边形 IoU。纯几何，不碰 Qt / rasterio。

为什么 point_recall 的分母是"被影像覆盖的点位"而不是"真值点位总数"
------------------------------------------------------------------
这是本模块唯一容易做错、做错了又特别好看的地方。

ICIMOD 研究区内有 55 个溃决湖点位，本地影像只覆盖其中 1/55。如果拿 55 当分母，
召回率会变成 1.8%，看起来像是"算法几乎什么都检不出来"——但真正的原因是**没去取影像**。
把取数失败和提取失败混进同一个比率，等于让数据获取的问题替算法背锅，也让改进算法
这件事在指标上完全看不出来。

所以这里把两件事拆开报：

    coverage_rate = n_covered / n_truth   影像覆盖了多少点位（取数环节）
    point_recall  = n_matched / n_covered 覆盖到的点位里，有多少检出（算法环节）

两者相乘才是"相对于全部真值的检出率"。想要后者，让调用方自己乘——
乘出来的数字含义清楚，而藏一个混合分母在函数里，三个月后没人说得清它是什么。

另外，ICIMOD 是**历史溃决事件**库，溃决后湖体可能已经排空。所以 point_recall
测的是"当前仍存在水体的湖有没有被检出"，不能叫"GLOF 识别准确率"。
这个措辞限制写在 evaluate_points() 的 notes 里，随报告一起落盘。

距离与面积的口径
----------------
* 点位距离用 haversine（球面，R=6371.0088 km，IUGG 平均半径）。实测在克亚吉尔
  （35.677°N）向东 0.01° 处，haversine 得 903.25 m、椭球大地线 905.30 m，
  偏差 -0.23%，远小于评价半径 150 m 的不确定度，且不引入额外依赖。
* 多边形面积用 pyproj.Geod 在 WGS84 椭球上直接算。不用"投影到 UTM 再算面积"：
  投影会带来带内变形（UTM 尺度因子最大 0.1%），而 Geod 是精确的，代码也更短。
  **更关键的是不能用度数**：0.01°×0.01° 的方块在赤道是 1.231 km²，
  在 36°N 只有 1.000 km²，差 23%；直接拿度数面积算 IoU，高纬度会系统性偏。

分层约束：本模块只依赖 numpy / shapely / pyproj。LakeRecord 只在类型标注里用到，
放到 TYPE_CHECKING 里导入——否则 `from .water import LakeRecord` 会把 rasterio
一起拉进来，纯几何模块就不纯了。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

import numpy as np
from pyproj import CRS, Geod, Transformer
from shapely import make_valid
from shapely import transform as _coords_transform
from shapely import wkt as _wkt
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import nearest_points

if TYPE_CHECKING:  # 只用于标注；运行时不导入 water，避免间接依赖 rasterio
    from .water import LakeRecord

__all__ = [
    "PointMatch",
    "match_points",
    "ConfusionCounts",
    "confusion_from_masks",
    "iou_of_polygons",
    "EvaluationReport",
    "evaluate_points",
]

# 面积一律在椭球上算。WGS84 是项目所有影像的基准，不提供换椭球的参数——
# 评价口径不该有隐藏开关。
_GEOD = Geod(ellps="WGS84")

# IUGG 平均地球半径。用球面近似而不是逐点椭球大地线，是因为点位匹配的判据
# 是 150 m 量级的半径，球面近似误差 <0.3%，不值得为此多一层投影。
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点大圆距离（km）。经度差先归一到 [-180, 180)，跨日界线不会算成绕地球一圈。"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians((lon2 - lon1 + 180.0) % 360.0 - 180.0)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    # 浮点误差可能让 a 略大于 1（对跖点），asin 会返回 nan，先夹住。
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class PointMatch:
    """一个真值点位与提取结果的匹配。

    distance_km 为 None 表示**一个水体都没提取到**，不是"距离 0"。
    这是 §8.5 的诚实性规则：缺数据就是 None，凡是用 0 冒充"刚好命中"的地方，
    最后都会在报告里变成一个好看到不真实的召回率。
    """

    site_id: str
    truth_lat: float
    truth_lon: float
    distance_km: float | None
    matched: bool
    lake_id: int | None
    lake_area_km2: float | None


def _local_frame(lon0: float, lat0: float) -> tuple[Transformer, Transformer]:
    """以点位为中心的方位等距投影（AEQD），用于量"点到多边形边界"的距离。

    AEQD 从中心点出发的距离是等距的，所以投影里量的米数就是地面距离；
    而且多边形在平面里做 nearest_points 比在球面上迭代简单得多。
    """
    crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat0!r} +lon_0={lon0!r} +datum=WGS84 +units=m +no_defs"
    )
    fwd = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)
    inv = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
    return fwd, inv


def _to_local(geom: BaseGeometry, fwd: Transformer) -> BaseGeometry:
    def fn(coords: np.ndarray) -> np.ndarray:
        x, y = fwd.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return _coords_transform(geom, fn)


def _lake_geometry(record: "LakeRecord") -> BaseGeometry | None:
    """取 LakeRecord 的 WGS84 几何；字段缺失或坏掉返回 None（调用方退回质心）。"""
    raw = getattr(record, "geometry_wgs84_json", None)
    if not raw:
        return None
    try:
        geom = shape(json.loads(raw))
    except Exception:
        return None
    if geom.is_empty:
        return None
    if not geom.is_valid:
        # 自相交的环（矢量化在窄颈处偶尔产生）会让 distance 抛拓扑异常。
        geom = make_valid(geom)
    return geom if not geom.is_empty else None


def _distance_to_record_km(lat: float, lon: float, record: "LakeRecord") -> float | None:
    """点位到"最近水体"的距离。

    量的是到**多边形边界**的距离，不是到质心的距离。理由：一个 2 km 长、300 m 宽的
    冰川湖，质心可能在点位 1 km 外，而湖岸就在点位脚下；按质心判会把明明提取对了的
    湖判成未命中。点在多边形内部时距离为 0（已经站在水面上，无所谓"离水多远"）。

    只有几何字段不可用时才退回质心距离，并在文档里明说这是降级口径。
    """
    geom = _lake_geometry(record)
    if geom is not None:
        fwd, inv = _local_frame(lon, lat)
        local = _to_local(geom, fwd)
        origin = Point(0.0, 0.0)
        if local.distance(origin) == 0.0:
            return 0.0
        near = nearest_points(local, origin)[0]
        nlon, nlat = inv.transform(near.x, near.y)
        # 用 haversine 而不是投影里的米数：报告里的距离始终是同一个口径，
        # 换成质心降级路径时数字也还可比。
        return haversine_km(lat, lon, nlat, nlon)

    clat = getattr(record, "centroid_lat", None)
    clon = getattr(record, "centroid_lon", None)
    if clat is None or clon is None:
        return None
    clat, clon = float(clat), float(clon)
    if not (math.isfinite(clat) and math.isfinite(clon)):
        return None
    return haversine_km(lat, lon, clat, clon)


def match_points(
    truth: Sequence[tuple[str, float, float]],
    records: Sequence["LakeRecord"],
    *,
    radius_km: float,
) -> list[PointMatch]:
    """按大圆距离把真值点位匹配到最近的水体。

    radius_km 必须由调用方显式给——半径是评价口径的一部分，藏一个默认值就不可复现，
    换个半径重跑得到的召回率也就没法对比。半径 <= 0 直接报错。

    没有 records 时，每个点位返回 distance_km=None / matched=False，
    不返回距离 0，也不抛异常——"没提取到"是一种结果，不是程序错误。
    """
    radius_km = float(radius_km)
    if not math.isfinite(radius_km) or radius_km <= 0.0:
        raise ValueError(f"radius_km 必须是正数，收到 {radius_km!r}")

    matches: list[PointMatch] = []
    for site_id, lat, lon in truth:
        lat, lon = float(lat), float(lon)
        best_km: float | None = None
        best = None
        for rec in records:
            dist = _distance_to_record_km(lat, lon, rec)
            if dist is None:
                continue
            if best_km is None or dist < best_km:
                best_km, best = dist, rec
        matches.append(
            PointMatch(
                site_id=str(site_id),
                truth_lat=lat,
                truth_lon=lon,
                distance_km=best_km,
                matched=best_km is not None and best_km <= radius_km,
                lake_id=int(best.lake_id) if best is not None else None,
                lake_area_km2=float(best.area_km2) if best is not None else None,
            )
        )
    return matches


def _ratio(numerator: float, denominator: float) -> float:
    """分母为 0 时返回 0.0。

    这里不返回 nan，是因为报告要直接写进 JSON（nan 不是合法 JSON）；也不返回 1.0，
    那等于把"没有样本"说成"全对"。0.0 是三者里唯一不会误导的取值，
    但调用方必须结合样本数一起看——见 EvaluationReport 的 notes。
    """
    return float(numerator) / float(denominator) if denominator else 0.0


@dataclass
class ConfusionCounts:
    """像元级混淆矩阵。四个计数都是整数像元数，比率一律现算，不落盘。"""

    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int

    @property
    def precision(self) -> float:
        """TP / (TP + FP)：提取出来的水里有多少是真的。"""
        return _ratio(self.true_positive, self.true_positive + self.false_positive)

    @property
    def recall(self) -> float:
        """TP / (TP + FN)：真水里提取出了多少。与小目标的检出能力直接挂钩。"""
        return _ratio(self.true_positive, self.true_positive + self.false_negative)

    @property
    def f1(self) -> float:
        """精确率与召回率的调和平均；两者任一为 0 时 f1 也为 0。"""
        p, r = self.precision, self.recall
        return _ratio(2.0 * p * r, p + r)

    @property
    def iou(self) -> float:
        """TP / (TP + FP + FN)，与 Jaccard 一致，也等于 交/并 的像元计数版。

        注意它与 true_negative 无关：大片非水背景会让 TN 极大，
        任何把 TN 放进分母的指标（如总体精度）在湖提取任务上都会虚高到 99%，
        所以本模块不提供总体精度。
        """
        return _ratio(self.true_positive, self.true_positive + self.false_positive + self.false_negative)


def confusion_from_masks(predicted: np.ndarray, truth: np.ndarray) -> ConfusionCounts:
    """像元级混淆矩阵。形状不一致直接 ValueError，不静默 reshape。

    静默对齐两个不同形状的数组，最坏情况是把 (h, w) 和 (w, h) 的转置掩膜当成同一景，
    算出一个看起来完全正常的 IoU。这种错误在报告里查不出来，只能在这里拦住。
    数组按 bool 解释：NDWI 掩膜、0/1 整型、浮点掩膜都能直接传。
    """
    pred = np.asarray(predicted, dtype=bool)
    tru = np.asarray(truth, dtype=bool)
    if pred.shape != tru.shape:
        raise ValueError(f"预测掩膜与真值掩膜形状不一致：{pred.shape} vs {tru.shape}，拒绝静默 reshape")
    return ConfusionCounts(
        true_positive=int(np.count_nonzero(pred & tru)),
        false_positive=int(np.count_nonzero(pred & ~tru)),
        false_negative=int(np.count_nonzero(~pred & tru)),
        true_negative=int(np.count_nonzero(~pred & ~tru)),
    )


def _geodesic_area_m2(geom: BaseGeometry | None) -> float:
    """WGS84 椭球上的面积（m²）。空几何为 0。

    必须先用 orient(geom, 1.0) 统一绕向。Geod 的有符号面积跟环的方向走：
    外环顺时针会得到负数；**洞的绕向若与外环相同，洞会被加进面积而不是减掉**——
    实测一个含 0.6°×0.6° 洞的方块，洞与外环同向时面积从 7878 km² 变成 16740 km²，
    没有任何异常，只是一个错得很安静的数字。orient 把外环定为逆时针、洞定为顺时针。
    """
    if geom is None or geom.is_empty:
        return 0.0
    return abs(_GEOD.geometry_area_perimeter(orient(geom, 1.0))[0])


def _parse_polygon(text: str, role: str) -> BaseGeometry:
    try:
        geom = _wkt.loads(text)
    except Exception as exc:  # shapely 抛的是 GEOSException，统一转成 ValueError
        raise ValueError(f"{role} WKT 无法解析：{exc}") from exc
    if not geom.is_valid:
        geom = make_valid(geom)
    return geom


def iou_of_polygons(pred_wkt: str, truth_wkt: str) -> float:
    """两个 WGS84 多边形的 IoU（shapely + pyproj Geod）。

    面积在椭球上算，不用平面度数：0.01°×0.01° 的方块在赤道 1.231 km²、
    在 36°N 1.000 km²，差 23%，用度数算 IoU 会在这个量级上系统性偏。
    也不投影到 UTM：Geod 是精确解，投影还多一层带内尺度变形（最大 0.1%）。

    真值为空 → ValueError（没有真值就没有 IoU，不能拿"没标注"当"全错"给 0 分）；
    预测为空 → 0.0（确实一个像元都没提出来的系统，得分就是 0）。
    """
    pred = _parse_polygon(pred_wkt, "预测")
    truth = _parse_polygon(truth_wkt, "真值")
    if truth.is_empty:
        raise ValueError("真值多边形为空，IoU 无定义：没有面真值就不该用 IoU 的名义报告")
    if pred.is_empty:
        return 0.0
    union_area = _geodesic_area_m2(pred.union(truth))
    if union_area <= 0.0:
        return 0.0  # 两个零面积的退化几何（纯线/点），IoU 无从谈起
    return _geodesic_area_m2(pred.intersection(truth)) / union_area


@dataclass
class EvaluationReport:
    """一次点位级评价的完整结果。字段与 accuracy.json 一一对应。"""

    n_truth: int
    n_covered: int  # 点位落在影像范围内的数量
    n_matched: int
    coverage_rate: float  # n_covered / n_truth
    point_recall: float  # n_matched / n_covered（分母是覆盖数，不是总数）
    radius_km: float
    matches: list[PointMatch]
    pixel_counts: ConfusionCounts | None = None
    notes: list[str] = field(default_factory=list)


def evaluate_points(
    truth: Sequence[tuple[str, float, float]],
    records: Sequence["LakeRecord"],
    *,
    radius_km: float,
    scene_bounds: tuple[float, float, float, float] | None = None,
) -> EvaluationReport:
    """点位级评价。

    scene_bounds 给 (lon_min, lat_min, lon_max, lat_max) 时，落在范围外的点位
    **不计入任何分母**，只在 notes 里记下排除了几个，也不进入 matches——
    它们没有参与评价，出现在 matches 里会让 n_matched 与 matches 的计数对不上。
    scene_bounds=None 时按全部点位算，并在 notes 里声明"未做覆盖过滤"。

    空输入不抛异常：没有真值点位时返回一份两项比率都是 0.0、带说明的报告。
    这里的 0.0 不是"准确率为零"，而是"没有可评价对象"——所以 notes 必须一起读。
    """
    radius_km = float(radius_km)
    if not math.isfinite(radius_km) or radius_km <= 0.0:
        raise ValueError(f"radius_km 必须是正数，收到 {radius_km!r}")

    truth = list(truth)
    n_truth = len(truth)
    notes: list[str] = []

    if scene_bounds is None:
        covered = truth
        notes.append(
            "未做覆盖过滤（scene_bounds=None）：全部点位按被影像覆盖处理，"
            "coverage_rate 恒为 1，point_recall 的分母等于真值总数"
        )
    else:
        lon_min, lat_min, lon_max, lat_max = (float(v) for v in scene_bounds)
        if lon_min > lon_max or lat_min > lat_max:
            raise ValueError(f"scene_bounds 顺序不对：{scene_bounds!r}，应为 (lon_min, lat_min, lon_max, lat_max)")
        covered = [
            (sid, lat, lon)
            for sid, lat, lon in truth
            if lon_min <= float(lon) <= lon_max and lat_min <= float(lat) <= lat_max
        ]
        n_excluded = n_truth - len(covered)
        notes.append(
            f"{n_excluded} 个点位落在场景范围外，已排除：不进入 matches，"
            f"也不计入 coverage_rate 与 point_recall 的分母"
        )

    n_covered = len(covered)
    matches = match_points(covered, records, radius_km=radius_km) if n_covered else []
    n_matched = sum(1 for m in matches if m.matched)

    if n_truth == 0:
        notes.append(
            "没有真值点位，无可评价对象：coverage_rate 与 point_recall 记 0.0——"
            "分母为零时记满分等于把'没测'说成'全对'"
        )
    elif n_covered == 0:
        notes.append(
            "所有点位都在场景范围外：point_recall 无分母，记 0.0。"
            "这属于取影像的覆盖问题，不是提取算法的失败"
        )
    elif not records:
        notes.append(
            "提取结果为空：所有覆盖点位的 distance_km 记为 None（未检出），不是 0"
        )

    notes.append(
        "ICIMOD HMA GLOF 数据库是历史溃决事件库，溃决后湖体可能已排空；"
        "point_recall 测的是'当前仍存在水体的湖有没有被检出'，不是 GLOF 识别准确率"
    )
    notes.append("本次只做点位级评价，未做边界精度评价（ICIMOD 无面真值，需人工标注样例才能报 iou）")

    return EvaluationReport(
        n_truth=n_truth,
        n_covered=n_covered,
        n_matched=n_matched,
        coverage_rate=_ratio(n_covered, n_truth),
        point_recall=_ratio(n_matched, n_covered),
        radius_km=radius_km,
        matches=matches,
        pixel_counts=None,
        notes=notes,
    )
