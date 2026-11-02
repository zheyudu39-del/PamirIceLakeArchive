"""暴露度模块验证：球面距离 -> 点到线/面 -> 三态缺失 -> 缓冲判定 -> 读入。

这个模块最容易出的错不是崩溃，是**把「不知道」写成「没有」**：

    * 没给居民点数据，却把 ``in_settlement_buffer`` 写成 False ——
      下游读成"查过了，附近没人"，而实际上一次都没查；
    * 拿平面欧氏距离代替球面距离 —— 36°N 处 1° 经度只有 90.2 km，
      当成 111 km 会让东西向距离系统性偏大 23%；
    * 点到道路只比顶点 —— 最近点通常落在两个顶点之间，会高估好几公里。

所以下面每条断言都盯着这三点。全部用合成 GeoJSON（临时目录，跑完即删），
不依赖任何真实地理数据。跑一遍约 1 秒。

用法：
    ./.venv/Scripts/python.exe scripts/verify_exposure.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glof.core import exposure as gexp  # noqa: E402
from glof.core.water import LakeRecord  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

# 球面近似的 1° 弧长（R = 6371.0088 km，与 accuracy.py 同口径）。
# 只用于"量级对不对"的断言；真值仍由 WGS84 大地线给出，两者相差约 0.2%。
SPHERE_DEG_KM = 111.195
# 任务书给的锚点：同一经度差 1° 纬度 ≈ 111.19 km。
LAT_1DEG_KM = 111.19


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def point_feature(lon: float, lat: float, **props) -> dict:
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def line_feature(coords, **props) -> dict:
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords}}


def polygon_feature(ring, **props) -> dict:
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def write_geojson(path: Path, features: list[dict]) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )


@dataclass
class FakeLake:
    """只带 assess_exposure 需要的三个属性，用来测"任意鸭子类型都能用"。"""

    lake_id: int
    centroid_lon: float
    centroid_lat: float


# --------------------------------------------------------------------------
def test_spherical_distance():
    """1° 纬差约 111.19 km；1° 经差要乘 cos(lat)。"""
    print("\n[1] 球面距离正确性")
    north = [point_feature(77.0, 37.0)]
    d = gexp.nearest_distance_km(77.0, 36.0, north)
    check("同经度 1° 纬差量到了", d is not None, repr(d))
    assert d is not None
    check("1° 纬差 ≈ 111.19 km，误差 < 0.5%",
          abs(d - LAT_1DEG_KM) / LAT_1DEG_KM < 0.005,
          f"{d:.3f} km（参考 {LAT_1DEG_KM} km，偏差 {abs(d - LAT_1DEG_KM) / LAT_1DEG_KM:.3%}）")
    # WGS84 椭球真值：36°N→37°N 的子午线弧长。这个数不是查来的，
    # 是独立数值积分 ∫ M(φ)dφ（M = a(1-e²)/(1-e²sin²φ)^1.5）得到的
    # 110.96830444 km，与 pyproj.Geod 相差 6e-10 km。
    # 容差必须收到 1 m：球面 haversine（R=6371.0088）会给出 111.195 km，
    # 差 227 m——放宽到 0.5% 的话，把椭球换成球面整套断言照样全过（实测如此）。
    check("是 WGS84 椭球大地线，不是球面近似（容差 1 m）",
          abs(d - 110.968304) < 0.001,
          f"{d:.6f} km（椭球 110.968304，球面 111.195，差 {abs(d - SPHERE_DEG_KM):.3f} km）")

    print("\n[2] 经度方向的距离随纬度收缩")
    east = [point_feature(78.0, 36.0)]
    d_lon = gexp.nearest_distance_km(77.0, 36.0, east)
    check("36°N 处 1° 经差量到了", d_lon is not None, repr(d_lon))
    assert d_lon is not None
    # 36°N 纬线上的 1° 弧长，同样由独立数值积分 ∫ N cosφ dλ 得到 90.16369 km，
    # pyproj 给 90.16329（差 0.4 m，大地线在纬线方向上比平行圈略短）。
    # 球面近似给 89.959 km，差 204 m——所以容差也收到 10 m。
    check("36°N 处 1° 经差 ≈ 90.163 km（椭球，容差 10 m）",
          abs(d_lon - 90.16329) < 0.010,
          f"{d_lon:.6f} km（球面参考 {SPHERE_DEG_KM * math.cos(math.radians(36.0)):.3f} km）")
    check("明显小于 1° 纬差（不是 111 km）", d_lon < 95.0, f"{d_lon:.3f} km")
    ratio = d_lon / d
    check("经/纬距离比 ≈ cos(36°) = 0.809",
          abs(ratio - math.cos(math.radians(36.0))) < 0.01, f"{ratio:.4f}")

    # 赤道上的同距离作为对照：经度收缩只在纬度上发生
    d_eq = gexp.nearest_distance_km(0.0, 0.0, [point_feature(1.0, 0.0)])
    check("赤道 1° 经差 ≈ 111.3 km（收缩只发生在高纬）",
          d_eq is not None and 110.5 < d_eq < 111.7, f"{d_eq:.3f} km" if d_eq else "None")


def test_point_to_line():
    """最近点落在顶点之间时，不能退回端点距离。"""
    print("\n[3] 点到线段：最近点落在两个顶点之间")
    # 77.00–77.10°E、lat 36 的一段路（约 9 km 长）
    road = [line_feature([[77.00, 36.00], [77.10, 36.00]])]
    d = gexp.nearest_distance_km(77.05, 36.01, road)
    check("量到了", d is not None, repr(d))
    assert d is not None
    # 垂距 ≈ 0.01° 纬差 ≈ 1.11 km
    check("垂距 ≈ 1.11 km", abs(d - 1.11) < 0.1, f"{d:.3f} km")
    # 到最近端点的距离 ≈ sqrt(4.5² + 1.11²) ≈ 4.65 km；若只比顶点会得到这个数
    check("不是到最近顶点的距离（否则会是约 4.65 km）", d < 2.0, f"{d:.3f} km")

    # 点在线段延长线外：最近点退回端点
    d_out = gexp.nearest_distance_km(77.20, 36.00, road)
    check("线段外的点退回端点距离", d_out is not None and abs(d_out - 8.94) < 0.3,
          f"{d_out:.3f} km" if d_out else "None")

    # 点在线上：距离为 0，不是 None。用子午线做例子——同纬度的两点之间
    # 大圆弧会向极点方向拱起（36°N、0.1° 跨度时中点约偏北 1.1 m），
    # 所以"沿纬线画一条线"并不是严格的大圆弧，不能拿来断言 0。
    meridian = [line_feature([[77.00, 36.00], [77.00, 36.10]])]
    d_on = gexp.nearest_distance_km(77.00, 36.05, meridian)
    check("点正好在线上时距离 = 0.0（不是 None）", d_on is not None and d_on < 1e-6,
          repr(d_on))
    # 纬线方向的同一条线上，中点确实会偏离约 1 m——这是几何事实，不是误差
    d_parallel = gexp.nearest_distance_km(77.05, 36.00, road)
    check("沿纬线的「直线」实际是大圆弧，中点偏离约 1 m",
          d_parallel is not None and 0.0 < d_parallel < 0.005, f"{d_parallel * 1000:.2f} m")

    # 多要素：取所有要素的最小值
    multi = [line_feature([[77.00, 36.00], [77.10, 36.00]]),
             line_feature([[77.05, 36.02], [77.05, 36.10]])]
    d_multi = gexp.nearest_distance_km(77.05, 36.01, multi)
    check("多要素取最近的（第二条线 0.01° ≈ 1.11 km）",
          d_multi is not None and abs(d_multi - 1.11) < 0.1, f"{d_multi:.3f} km")

    # MultiLineString / MultiPoint 与拆开的单要素结果一致
    mls = {"type": "Feature", "properties": {}, "geometry": {
        "type": "MultiLineString",
        "coordinates": [[[77.00, 36.00], [77.10, 36.00]],
                        [[77.05, 36.02], [77.05, 36.10]]]}}
    d_mls = gexp.nearest_distance_km(77.05, 36.01, [mls])
    check("MultiLineString 结果与拆开一致",
          d_mls is not None and abs(d_mls - d_multi) < 1e-9, f"{d_mls:.3f} km")
    mp = {"type": "Feature", "properties": {}, "geometry": {
        "type": "MultiPoint", "coordinates": [[78.0, 36.0], [77.02, 36.0]]}}
    check("MultiPoint 取最近的那个点",
          abs(gexp.nearest_distance_km(77.0, 36.0, [mp]) - 1.803) < 0.05,
          f"{gexp.nearest_distance_km(77.0, 36.0, [mp]):.3f} km")


def test_point_in_polygon():
    """点在面内时距离为 0，不是到边界的距离。"""
    print("\n[4] 点到面：面内为 0")
    square = polygon_feature([[77.0, 36.0], [77.1, 36.0], [77.1, 36.1],
                              [77.0, 36.1], [77.0, 36.0]])
    d_in = gexp.nearest_distance_km(77.05, 36.05, [square])
    check("面内距离 = 0.0", d_in is not None and d_in == 0.0, repr(d_in))
    d_out = gexp.nearest_distance_km(77.05, 36.20, [square])
    check("面外距离 = 到最近边的距离 ≈ 0.1° 纬差 ≈ 11.1 km",
          d_out is not None and abs(d_out - 11.1) < 0.3,
          f"{d_out:.3f} km" if d_out else "None")

    # 带洞的面：点在洞里，最近边是洞的边。洞是 0.02° 见方，
    # 36°N 处东西向 0.02° ≈ 1.80 km、南北向 ≈ 2.22 km，最近的是东西向的洞边。
    hole = polygon_feature(
        [[77.0, 36.0], [77.2, 36.0], [77.2, 36.2], [77.0, 36.2], [77.0, 36.0]],
    )
    hole["geometry"]["coordinates"].append(
        [[77.08, 36.08], [77.12, 36.08], [77.12, 36.12], [77.08, 36.12], [77.08, 36.08]]
    )
    d_hole = gexp.nearest_distance_km(77.10, 36.10, [hole])
    check("点在洞里时量到洞边的距离（≈1.8 km，不是外环 11 km）",
          d_hole is not None and abs(d_hole - 1.8) < 0.1,
          f"{d_hole:.3f} km" if d_hole else "None")


def test_empty_is_none():
    print("\n[5] 空要素 -> None（不是 0）")
    check("空列表返回 None", gexp.nearest_distance_km(77.0, 36.0, []) is None)
    check("None 返回 None", gexp.nearest_distance_km(77.0, 36.0, None) is None)
    no_geom = [{"type": "Feature", "properties": {}, "geometry": None}]
    check("要素全是空几何返回 None", gexp.nearest_distance_km(77.0, 36.0, no_geom) is None)
    empty_fc = {"type": "FeatureCollection", "features": []}
    check("空 FeatureCollection 返回 None",
          gexp.nearest_distance_km(77.0, 36.0, empty_fc) is None)
    # 空要素的评估结果：距离 None，缓冲判定也必须是 None
    recs = gexp.assess_exposure([FakeLake(1, 77.0, 36.0)], roads_path=_tmp_empty_geojson(),
                                buffer_km=5.0)
    check("道路文件为空时 road_distance_km = None", recs[0].road_distance_km is None,
          repr(recs[0].road_distance_km))
    check("道路文件为空时 in_road_buffer = None（不是 False）",
          recs[0].in_road_buffer is None, repr(recs[0].in_road_buffer))

    # NaN 距离绝不能落进 False：`nan <= buffer` 在 Python 里是 False，
    # 于是"距离没量出来"会被下游读成"量到了，且不在范围内"。
    # 这是本项目最危险的错误形态，必须两条都堵住。
    check("NaN 距离的缓冲判定是 None（不是 False）",
          gexp._in_buffer(float("nan"), 5.0) is None, repr(gexp._in_buffer(float("nan"), 5.0)))
    nan_geom = [{"type": "Feature", "properties": {},
                 "geometry": {"type": "Point", "coordinates": [float("nan"), 36.1]}}]
    try:
        value = gexp.nearest_distance_km(77.0, 36.0, nan_geom)
        ok = value is not None and math.isfinite(value)
        detail = f"得到 {value!r}"
    except ValueError:
        ok = True
        detail = "抛 ValueError"
    check("几何含 NaN 时不会安静地返回 nan 距离", ok, detail)


def test_settlement_tristate():
    """没给居民点数据时，三个字段都是 None，不是 False。"""
    print("\n[6] 缺居民点数据：None，不是 False")
    roads = _tmp_geojson([line_feature([[77.0, 36.0], [77.0, 36.5]])])
    lakes = [FakeLake(1, 77.01, 36.1)]

    recs = gexp.assess_exposure(lakes, roads_path=roads, buffer_km=5.0)
    r = recs[0]
    check("没给 settlements_path 时 settlement_distance_km = None",
          r.settlement_distance_km is None, repr(r.settlement_distance_km))
    check("没给 settlements_path 时 in_settlement_buffer = None（不是 False）",
          r.in_settlement_buffer is None, repr(r.in_settlement_buffer))
    check("道路字段照常算出来", r.road_distance_km is not None,
          f"{r.road_distance_km:.3f} km")
    check("道路判定是布尔值，不是 None", isinstance(r.in_road_buffer, bool),
          repr(r.in_road_buffer))

    # 给了路径但文件是空集合：同样是"没有数据"
    empty = _tmp_empty_geojson()
    r2 = gexp.assess_exposure(lakes, roads_path=roads, settlements_path=empty,
                              buffer_km=5.0)[0]
    check("居民点文件为空时也是 None", r2.settlement_distance_km is None
          and r2.in_settlement_buffer is None,
          f"{r2.settlement_distance_km!r} / {r2.in_settlement_buffer!r}")

    # 真有数据时才出现 False（"量到了且不在范围内"）
    far = _tmp_geojson([point_feature(78.5, 36.1)])
    r3 = gexp.assess_exposure(lakes, roads_path=roads, settlements_path=far,
                              buffer_km=5.0)[0]
    check("居民点很远时是 False（量到了，不在范围内）",
          r3.in_settlement_buffer is False, repr(r3.in_settlement_buffer))
    check("居民点距离量到了", r3.settlement_distance_km is not None,
          f"{r3.settlement_distance_km:.3f} km")


def test_buffer_boundary():
    """距离 <= buffer 判为范围内，边界上算命中。"""
    print("\n[7] 缓冲判定：边界内外")
    roads = _tmp_geojson([point_feature(77.0, 36.01)])
    lake = [FakeLake(1, 77.0, 36.0)]
    d = gexp.nearest_distance_km(77.0, 36.0, gexp.load_features(roads))
    assert d is not None
    check("先量出基准距离", d > 0, f"{d:.6f} km")

    r_eq = gexp.assess_exposure(lake, roads_path=roads, buffer_km=d)[0]
    check("距离 == buffer 时判为范围内（<= 而非 <）", r_eq.in_road_buffer is True,
          f"d={d:.6f} buffer={d:.6f}")

    r_in = gexp.assess_exposure(lake, roads_path=roads, buffer_km=d * 1.001)[0]
    check("buffer 略大时判为范围内", r_in.in_road_buffer is True)

    r_out = gexp.assess_exposure(lake, roads_path=roads, buffer_km=d * 0.999)[0]
    check("buffer 略小时判为范围外（False，不是 None）", r_out.in_road_buffer is False,
          repr(r_out.in_road_buffer))

    # buffer 没有默认值：漏传必须 TypeError，不能悄悄用 5 km 出一个判定
    try:
        gexp.assess_exposure(lake, roads_path=roads)
        ok = False
    except TypeError:
        ok = True
    check("漏传 buffer_km 抛 TypeError（阈值不藏默认值）", ok)

    # buffer 非法必须报错，不能悄悄兜底
    for bad in (0.0, -1.0, float("nan")):
        try:
            gexp.assess_exposure(lake, roads_path=roads, buffer_km=bad)
            ok = False
        except ValueError:
            ok = True
        check(f"buffer_km={bad!r} 抛 ValueError", ok)


def test_nearest_of_many():
    print("\n[8] 多要素取最近的那个")
    features = [
        point_feature(78.0, 36.0, name="远"),
        point_feature(77.02, 36.0, name="近"),
        point_feature(77.10, 36.0, name="中"),
    ]
    d = gexp.nearest_distance_km(77.0, 36.0, features)
    d_near = gexp.nearest_distance_km(77.0, 36.0, [features[1]])
    check("结果是最近那个要素的距离", d is not None and abs(d - d_near) < 1e-9,
          f"{d:.3f} km")
    check("不是先遇到的那个（约 90 km）", d < 5.0, f"{d:.3f} km")

    # 传裸几何也要能算。之前裸几何被当成"没有数据"返回 None——
    # 而 load_features() 明明接受顶层裸几何，两条入口口径不一致。
    bare = {"type": "Point", "coordinates": [77.02, 36.0]}
    d_bare = gexp.nearest_distance_km(77.0, 36.0, bare)
    check("裸几何（没有 Feature 包装）也能量到",
          d_bare is not None and abs(d_bare - d_near) < 1e-9,
          repr(d_bare))

    # 把路径当成要素传进来，必须报错。否则字符串被逐字符迭代、一个 Mapping 都没有，
    # 结果悄悄变成 None，调用方读成"研究区没有道路"。
    try:
        gexp.nearest_distance_km(77.0, 36.0, "data/roads.geojson")
        ok = False
    except TypeError:
        ok = True
    check("把路径当要素传进来抛 TypeError（不是悄悄返回 None）", ok)
    try:
        gexp.nearest_distance_km(77.0, 36.0, Path("data/roads.geojson"))
        ok = False
    except TypeError:
        ok = True
    check("Path 对象当要素传进来同样抛 TypeError", ok)


def test_load_features():
    print("\n[9] load_features 读入")
    try:
        gexp.load_features(ROOT / "不存在的文件.geojson")
        ok = False
    except FileNotFoundError:
        ok = True
    check("文件不存在抛 FileNotFoundError", ok)

    # 文件存在但不是 JSON
    bad = _tmp_file("bad.geojson", "这不是 json")
    try:
        gexp.load_features(bad)
        ok = False
    except ValueError:
        ok = True
    check("非法 JSON 抛 ValueError", ok)

    # properties 原样保留
    path = _tmp_geojson([line_feature([[77.0, 36.0], [77.1, 36.1]],
                                      highway="trunk", name="G314")])
    feats = gexp.load_features(path)
    check("读回 1 个要素", len(feats) == 1, f"{len(feats)}")
    check("properties 原样保留",
          feats[0]["properties"].get("highway") == "trunk"
          and feats[0]["properties"].get("name") == "G314",
          str(feats[0]["properties"]))
    check("geometry 原样保留", feats[0]["geometry"]["type"] == "LineString")

    # 顶层直接是几何的简单 GeoJSON
    simple = _tmp_file("simple.geojson", json.dumps(
        {"type": "Point", "coordinates": [77.0, 36.0]}))
    feats_simple = gexp.load_features(simple)
    check("顶层是裸 Point 也能读", len(feats_simple) == 1
          and feats_simple[0]["geometry"]["type"] == "Point")

    # 文件存在但要素为空：返回空列表，不是抛异常——"没有要素"才是真正的没数据
    check("空 FeatureCollection 读成空列表",
          gexp.load_features(_tmp_empty_geojson()) == [])

    # 顶层类型不认识：报错，不要猜
    weird = _tmp_file("weird.geojson", json.dumps({"type": "Table", "rows": []}))
    try:
        gexp.load_features(weird)
        ok = False
    except ValueError:
        ok = True
    check("不认识的顶层类型抛 ValueError", ok)

    # 经纬度写反：纬度 95 必须报错，而不是算出一个数
    # 写成 [纬度, 经度] 时纬度 95 会落在经度位、经度 36 落在纬度位，
    # 这里直接把纬度放到 95 复现"把度数写反"的后果
    swapped = _tmp_geojson([point_feature(36.0, 95.0)])
    try:
        gexp.load_features(swapped)
        ok = False
    except ValueError:
        ok = True
    check("经度越界（可能经纬度写反）抛 ValueError", ok)

    # GeoJSON 没有图层：传 layer 必须报错，不能静默忽略
    try:
        gexp.load_features(path, layer="roads")
        ok = False
    except ValueError:
        ok = True
    check("GeoJSON 传 layer 抛 ValueError（不静默忽略）", ok)


def test_assess_with_lake_record():
    """用真的 LakeRecord 跑一遍，防止字段名漂移。"""
    print("\n[10] 与 water.LakeRecord 对接")
    roads = _tmp_geojson([line_feature([[77.00, 36.00], [77.10, 36.00]])])
    settlements = _tmp_geojson([point_feature(77.05, 36.03)])
    record = LakeRecord(
        lake_id=7, area_m2=1.0e6, perimeter_m=4000.0, n_pixels=100,
        area_px_m2=1.0e4, mean_ndwi=0.5,
        centroid_lon=77.05, centroid_lat=36.01, geometry_wgs84_json="{}",
    )
    out = gexp.assess_exposure([record], roads_path=roads,
                               settlements_path=settlements, buffer_km=5.0)
    check("返回一条记录", len(out) == 1)
    r = out[0]
    check("lake_id 对上", r.lake_id == 7, repr(r.lake_id))
    check("道路距离 ≈ 1.11 km", r.road_distance_km is not None
          and abs(r.road_distance_km - 1.11) < 0.1,
          f"{r.road_distance_km:.3f} km")
    check("居民点距离 ≈ 2.22 km", r.settlement_distance_km is not None
          and abs(r.settlement_distance_km - 2.22) < 0.1,
          f"{r.settlement_distance_km:.3f} km")
    check("两项都在 5 km 内", r.in_road_buffer is True and r.in_settlement_buffer is True)
    check("to_properties 带齐五个字段",
          set(r.to_properties()) == {
              "lake_id", "exposure_road_distance_km", "exposure_settlement_distance_km",
              "exposure_in_road_buffer", "exposure_in_settlement_buffer"},
          str(sorted(r.to_properties())))
    check("describe 里 None 写成「无数据」",
          "无数据" in gexp.ExposureRecord(1).describe(),
          gexp.ExposureRecord(1).describe())

    # 顺序与输入一致
    lakes = [FakeLake(3, 77.05, 36.01), FakeLake(1, 78.0, 36.5)]
    out2 = gexp.assess_exposure(lakes, roads_path=roads, buffer_km=5.0)
    check("输出顺序与输入一致", [r.lake_id for r in out2] == [3, 1],
          str([r.lake_id for r in out2]))
    check("远的那个不在缓冲区内", out2[1].in_road_buffer is False,
          repr(out2[1].in_road_buffer))

    # 湖心坐标不可用（NaN）时：距离 None，判定 None
    bad_lake = [FakeLake(9, float("nan"), 36.0)]
    r9 = gexp.assess_exposure(bad_lake, roads_path=roads, buffer_km=5.0)[0]
    check("湖心坐标无效时距离是 None，不是 0",
          r9.road_distance_km is None and r9.in_road_buffer is None,
          f"{r9.road_distance_km!r} / {r9.in_road_buffer!r}")


def test_no_qt():
    print("\n[11] 分层约束")
    src = (ROOT / "glof" / "core" / "exposure.py").read_text(encoding="utf-8")
    check("core/exposure.py 不 import Qt",
          not any(tok in src for tok in ("PySide6", "PyQt", "QtCore", "QtGui", "QtWidgets")))


# --------------------------------------------------------------------------
# 临时文件
# --------------------------------------------------------------------------
_TMPDIR: tempfile.TemporaryDirectory | None = None


def _tmp_dir() -> Path:
    global _TMPDIR
    if _TMPDIR is None:
        _TMPDIR = tempfile.TemporaryDirectory(prefix="glof_exposure_")
    return Path(_TMPDIR.name)


def _tmp_file(name: str, text: str) -> Path:
    p = _tmp_dir() / name
    p.write_text(text, encoding="utf-8")
    return p


def _tmp_geojson(features: list[dict]) -> Path:
    p = _tmp_dir() / f"f{len(list(_tmp_dir().iterdir()))}.geojson"
    write_geojson(p, features)
    return p


def _tmp_empty_geojson() -> Path:
    return _tmp_geojson([])


def main() -> int:
    print("=" * 70)
    print("暴露度模块验证（离线，合成 GeoJSON）")
    print("=" * 70)
    try:
        test_spherical_distance()
        test_point_to_line()
        test_point_in_polygon()
        test_empty_is_none()
        test_settlement_tristate()
        test_buffer_boundary()
        test_nearest_of_many()
        test_load_features()
        test_assess_with_lake_record()
        test_no_qt()
    finally:
        if _TMPDIR is not None:
            _TMPDIR.cleanup()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"失败 {len(FAILURES)} / {CHECKS[0]}：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"全部通过：{CHECKS[0]} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
