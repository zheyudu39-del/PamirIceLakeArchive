"""精度模块验证：大圆距离 -> 点位匹配 -> 召回口径 -> 球面 IoU -> 混淆矩阵。

每一节都至少有一条**能失败**的断言，断言值都是从别处独立算出来的
（GeoGeod 椭球解、解析几何、手数像元），不是把模块的输出再抄一遍。

为什么专门验证"分母是覆盖数"
----------------------------
这是整套评价里唯一一个写错了会让结果变好看的地方。构造 3 个真值点、其中 1 个在景外，
如果返回 matched/3 而不是 matched/2，指标会凭空降低，且没有任何报错。
所以这里显式断言 recall == 1/2 且 != 1/3。

用法：
    ./.venv/Scripts/python.exe scripts/verify_accuracy.py
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from pyproj import Geod  # noqa: E402
from shapely.geometry import box, mapping, shape  # noqa: E402

from glof.core import accuracy as gacc  # noqa: E402
from glof.core.water import LakeRecord  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]
GEOD = Geod(ellps="WGS84")

# 克亚吉尔冰川湖（2017-07-27 溃决）的点位，项目里所有验证都用它做锚点。
KYAGAR_LAT, KYAGAR_LON = 35.677371, 77.190788


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def expect_value_error(name: str, fn) -> None:
    """反向用例：喂错数据必须报 ValueError，而不是静默算出一个数。"""
    try:
        fn()
    except ValueError as exc:
        check(name, True, f"ValueError: {exc}")
    except Exception as exc:  # 抛了别的异常也算失败：契约要求的是 ValueError
        check(name, False, f"抛的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有报错，静默通过了")


def offset(lon: float, lat: float, azimuth: float, dist_m: float) -> tuple[float, float]:
    """沿方位角走 dist_m 米后的 (lon, lat)，用椭球大地线算——与 haversine 是两套算法。"""
    l, a, _ = GEOD.fwd(lon, lat, azimuth, dist_m)
    return l, a


def make_lake(lake_id: int, geom, *, area_m2: float | None = None) -> LakeRecord:
    """按 LakeRecord 的真实字段构造一个提取结果。"""
    c = geom.centroid
    if area_m2 is None:
        area_m2 = abs(GEOD.geometry_area_perimeter(geom)[0])
    return LakeRecord(
        lake_id=lake_id,
        area_m2=area_m2,
        perimeter_m=abs(GEOD.geometry_area_perimeter(geom)[1]),
        n_pixels=int(area_m2 / 100.0),
        area_px_m2=area_m2,
        mean_ndwi=0.4,
        centroid_lon=float(c.x),
        centroid_lat=float(c.y),
        geometry_wgs84_json=json.dumps(mapping(geom)),
    )


def square_wkt(west: float, south: float, size: float) -> str:
    e, n = west + size, south + size
    return (
        f"POLYGON (({west} {south}, {e} {south}, {e} {n}, {west} {n}, {west} {south}))"
    )


def main() -> int:
    print("=" * 74)
    print("步骤 1 / haversine 大圆距离")
    print("=" * 74)

    # 独立真值：椭球大地线（pyproj Geod）。球面近似应与它相差 <1%。
    east = gacc.haversine_km(KYAGAR_LAT, KYAGAR_LON, KYAGAR_LAT, KYAGAR_LON + 0.01)
    lon_a, lat_a, ellipsoid_m = GEOD.inv(KYAGAR_LON, KYAGAR_LAT, KYAGAR_LON + 0.01, KYAGAR_LAT)
    rel = (east * 1000.0 - ellipsoid_m) / ellipsoid_m
    print(f"  向东 0.01°：haversine {east * 1000:.2f} m，椭球大地线 {ellipsoid_m:.2f} m")
    check(
        "haversine 与椭球大地线偏差 < 1%（0.01° 东向）",
        abs(rel) < 0.01,
        f"相对偏差 {rel * 100:+.3f}%",
    )

    north = gacc.haversine_km(0.0, 77.0, 1.0, 77.0)
    lon_b, lat_b, ellipsoid_ns = GEOD.inv(77.0, 0.0, 77.0, 1.0)
    rel_ns = (north * 1000.0 - ellipsoid_ns) / ellipsoid_ns
    check(
        "haversine 与椭球大地线偏差 < 1%（赤道 1° 南北向）",
        abs(rel_ns) < 0.01,
        f"haversine {north:.3f} km vs 椭球 {ellipsoid_ns / 1000:.3f} km（{rel_ns * 100:+.3f}%）",
    )

    dateline = gacc.haversine_km(0.0, 179.995, 0.0, -179.995)
    check(
        "跨日界线的经度差不绕地球（1.112 km，不是 4 万 km）",
        abs(dateline - 1.1119) < 0.01,
        f"实际 {dateline:.4f} km",
    )
    same = gacc.haversine_km(KYAGAR_LAT, KYAGAR_LON, KYAGAR_LAT, KYAGAR_LON)
    check("同一点距离 = 0", same == 0.0, f"实际 {same}")

    print()
    print("=" * 74)
    print("步骤 2 / 点位匹配：距离量到多边形边界，半径判据可切换")
    print("=" * 74)

    # 构造一个南边缘距点位 100 m 的方块湖（用椭球大地线摆点，面积 400 m x 400 m）
    south_mid = offset(KYAGAR_LON, KYAGAR_LAT, 0.0, 100.0)
    sw = offset(*south_mid, 270.0, 200.0)
    se = offset(*south_mid, 90.0, 200.0)
    nw = offset(*sw, 0.0, 400.0)
    ne = offset(*se, 0.0, 400.0)
    near_lake = shape(
        {
            "type": "Polygon",
            "coordinates": [[sw, se, ne, nw, sw]],
        }
    )
    lake_100m = make_lake(1, near_lake)

    truth1 = [("site001", KYAGAR_LAT, KYAGAR_LON)]
    m_150 = gacc.match_points(truth1, [lake_100m], radius_km=0.15)[0]
    m_050 = gacc.match_points(truth1, [lake_100m], radius_km=0.05)[0]
    print(f"  点位到边界 {m_150.distance_km * 1000:.2f} m")
    check(
        "边界距离在 95–105 m 之间（构造值是 100 m）",
        m_150.distance_km is not None and 0.095 <= m_150.distance_km <= 0.105,
        f"实际 {m_150.distance_km * 1000:.2f} m",
    )
    check("radius_km=0.15 时命中", m_150.matched is True)
    check("radius_km=0.05 时不命中（同一数据，只换半径）", m_050.matched is False,
          f"distance {m_050.distance_km * 1000:.2f} m > 50 m")
    check(
        "命中记录带 lake_id 与面积",
        m_150.lake_id == 1 and m_150.lake_area_km2 is not None,
        f"lake_id={m_150.lake_id}, area={m_150.lake_area_km2:.4f} km2",
    )

    inside_lake = make_lake(2, box(KYAGAR_LON - 0.002, KYAGAR_LAT - 0.002,
                                   KYAGAR_LON + 0.002, KYAGAR_LAT + 0.002))
    m_in = gacc.match_points(truth1, [inside_lake], radius_km=0.05)[0]
    check("点位落在湖内时距离为 0（不是 None，也不是到质心的距离）",
          m_in.distance_km == 0.0 and m_in.matched is True, f"distance={m_in.distance_km}")

    # 长条湖：质心在 1.47 km 外，但湖岸就在点位脚下 103 m。
    # 若按质心匹配，这种明明提取对了的湖会被判成漏检——这就是量边界的原因。
    elongated = make_lake(3, box(77.19, 35.6783, 77.21, 35.6983))
    m_elong = gacc.match_points(truth1, [elongated], radius_km=0.15)[0]
    centroid_km = gacc.haversine_km(KYAGAR_LAT, KYAGAR_LON,
                                    elongated.centroid_lat, elongated.centroid_lon)
    check(
        "长条湖按边界命中，而按质心会漏检（质心距 > 半径）",
        m_elong.matched is True and m_elong.distance_km < 0.11 and centroid_km > 0.15,
        f"边界 {m_elong.distance_km * 1000:.0f} m < 半径 150 m < 质心 {centroid_km * 1000:.0f} m",
    )

    no_lake = gacc.match_points(truth1, [], radius_km=0.15)[0]
    check(
        "没有提取结果时 distance_km 为 None、matched=False（不是距离 0）",
        no_lake.distance_km is None and no_lake.matched is False and no_lake.lake_id is None,
    )

    # 缺几何字段的降级路径：退回质心距离
    broken = make_lake(4, near_lake)
    broken.geometry_wgs84_json = ""
    m_fallback = gacc.match_points(truth1, [broken], radius_km=0.5)[0]
    check(
        "几何字段缺失时退回质心距离（可算，且仍是有限值）",
        m_fallback.distance_km is not None and math.isfinite(m_fallback.distance_km),
        f"质心距 {m_fallback.distance_km * 1000:.0f} m",
    )

    print()
    print("=" * 74)
    print("步骤 3 / point_recall 的分母是覆盖数，不是真值总数")
    print("=" * 74)

    truth3 = [
        ("site001", KYAGAR_LAT, KYAGAR_LON),      # 景内，有湖
        ("site002", 35.700, 77.250),              # 景内，无湖
        ("site003", 30.000, 70.000),              # 景外
    ]
    bounds = (77.15, 35.60, 77.30, 35.75)
    rep = gacc.evaluate_points(truth3, [lake_100m], radius_km=0.15, scene_bounds=bounds)
    print(f"  n_truth={rep.n_truth} n_covered={rep.n_covered} n_matched={rep.n_matched} "
          f"coverage={rep.coverage_rate:.3f} recall={rep.point_recall:.3f}")
    check("景外点位不计入 n_covered（3 个点 -> 2 个）", rep.n_covered == 2, f"实际 {rep.n_covered}")
    check("coverage_rate = 2/3", abs(rep.coverage_rate - 2 / 3) < 1e-12,
          f"实际 {rep.coverage_rate:.6f}")
    check("point_recall = 1/2（分母是覆盖数）", abs(rep.point_recall - 0.5) < 1e-12,
          f"实际 {rep.point_recall:.6f}")
    check("point_recall 不是 1/3（分母若错用真值总数，指标会被稀释）",
          abs(rep.point_recall - 1 / 3) > 1e-6)
    check("景外点位不进入 matches（否则 n_matched 与 matches 计数对不上）",
          len(rep.matches) == 2 and all(m.site_id != "site003" for m in rep.matches),
          f"matches={[m.site_id for m in rep.matches]}")
    check("notes 写明排除了 1 个景外点位",
          any("1 个点位落在场景范围外" in n for n in rep.notes))
    check("notes 声明了 ICIMOD 是历史溃决库、不能叫 GLOF 识别准确率",
          any("不是 GLOF 识别准确率" in n for n in rep.notes))
    check("notes 声明未做边界精度评价", any("未做边界精度评价" in n for n in rep.notes))

    rep_all = gacc.evaluate_points(truth3, [lake_100m], radius_km=0.15, scene_bounds=None)
    check("scene_bounds=None 时 coverage_rate=1，并声明未做覆盖过滤",
          rep_all.coverage_rate == 1.0 and any("未做覆盖过滤" in n for n in rep_all.notes),
          f"n_covered={rep_all.n_covered}/{rep_all.n_truth}")

    rep_empty_lakes = gacc.evaluate_points(truth3[:2], [], radius_km=0.15, scene_bounds=bounds)
    check("提取结果为空时 point_recall=0，且 notes 说明是未检出而非距离 0",
          rep_empty_lakes.point_recall == 0.0
          and any("记为 None" in n for n in rep_empty_lakes.notes))

    print()
    print("=" * 74)
    print("步骤 4 / 多边形 IoU：面积在 WGS84 椭球上算")
    print("=" * 74)

    a = square_wkt(77.20, 35.60, 0.01)
    same_iou = gacc.iou_of_polygons(a, a)
    check("同一多边形 IoU = 1.0", abs(same_iou - 1.0) < 1e-12, f"实际 {same_iou:.12f}")

    far = square_wkt(77.30, 35.60, 0.01)
    far_iou = gacc.iou_of_polygons(a, far)
    check("互不相交 IoU = 0.0", far_iou == 0.0, f"实际 {far_iou}")

    half = square_wkt(77.205, 35.60, 0.01)
    half_iou = gacc.iou_of_polygons(half, a)
    # 两个等大正方形错开半边长：交 = 1/2，并 = 3/2，IoU 的解析值恰为 1/3。
    check("半重叠正方形 IoU = 1/3（解析值，不是 >0）",
          abs(half_iou - 1 / 3) < 1e-6, f"实际 {half_iou:.9f}，偏差 {half_iou - 1 / 3:+.2e}")

    quarter = square_wkt(77.205, 35.605, 0.01)
    quarter_iou = gacc.iou_of_polygons(quarter, a)
    # 四分之一重叠：交 = 1/4，并 = 2 - 1/4 = 7/4，IoU = 1/7。
    check("四分之一重叠正方形 IoU = 1/7（解析值）",
          abs(quarter_iou - 1 / 7) < 1e-6, f"实际 {quarter_iou:.9f}")

    # 带洞多边形：洞若被当成外环同向，面积会把洞加进去，IoU 会 >1。
    outer = box(0.0, 0.0, 1.0, 1.0)
    holed_wkt = (
        "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0), (0.2 0.2, 0.8 0.2, 0.8 0.8, 0.2 0.8, 0.2 0.2))"
    )
    area_outer = abs(GEOD.geometry_area_perimeter(outer)[0])
    area_hole = abs(GEOD.geometry_area_perimeter(box(0.2, 0.2, 0.8, 0.8))[0])
    expected_holed = 1.0 - area_hole / area_outer
    holed_iou = gacc.iou_of_polygons(holed_wkt, outer.wkt)
    check("带洞多边形的洞被减掉而不是加上（IoU = 1 - 洞/外环）",
          abs(holed_iou - expected_holed) < 1e-9 and holed_iou < 1.0,
          f"实际 {holed_iou:.9f}，期望 {expected_holed:.9f}")

    # 球面 vs 平面：同是 0.01° 的方块，36°N 的实际面积比赤道小约 19%。
    area_eq = gacc._geodesic_area_m2(box(0.0, 0.0, 0.01, 0.01))
    area_36 = gacc._geodesic_area_m2(box(0.0, 36.0, 0.01, 36.01))
    ratio = area_36 / area_eq
    cos36 = math.cos(math.radians(36.0))
    planar_ratio = box(0.0, 0.0, 0.01, 0.01).area / box(0.0, 36.0, 0.01, 36.01).area
    print(f"  面积：赤道 {area_eq / 1e6:.4f} km2，36°N {area_36 / 1e6:.4f} km2，"
          f"比值 {ratio:.6f}，cos36° = {cos36:.6f}")
    check(
        "球面面积比 = cos(36°)/cos(0°) ±1%（证明没有在用平面度数）",
        abs(ratio / cos36 - 1.0) < 0.01,
        f"比值/cos36° - 1 = {ratio / cos36 - 1:+.4%}；若按度数算比值会是 {planar_ratio:.6f}",
    )
    check(
        "平面度数面积在 36°N 与赤道完全相同（这正是不能用它算 IoU 的原因）",
        abs(planar_ratio - 1.0) < 1e-12,
        f"平面比值 {planar_ratio:.12f}",
    )

    print()
    print("=" * 74)
    print("步骤 5 / 像元混淆矩阵（含反向用例）")
    print("=" * 74)

    pred = np.array([[1, 1, 0], [0, 0, 1], [1, 0, 1]], dtype=bool)
    tru = np.array([[1, 0, 0], [0, 1, 1], [1, 1, 0]], dtype=bool)
    cc = gacc.confusion_from_masks(pred, tru)
    print(f"  TP={cc.true_positive} FP={cc.false_positive} "
          f"FN={cc.false_negative} TN={cc.true_negative}")
    check("手工枚举的四个计数正确（TP=3 FP=2 FN=2 TN=2）",
          (cc.true_positive, cc.false_positive, cc.false_negative, cc.true_negative) == (3, 2, 2, 2))
    check("四个计数之和 = 像元总数 9",
          cc.true_positive + cc.false_positive + cc.false_negative + cc.true_negative == pred.size)
    check("precision = 3/5", abs(cc.precision - 0.6) < 1e-12, f"实际 {cc.precision}")
    check("recall = 3/5", abs(cc.recall - 0.6) < 1e-12, f"实际 {cc.recall}")
    check("f1 = 0.6", abs(cc.f1 - 0.6) < 1e-12, f"实际 {cc.f1}")
    check("iou = 3/7（TP+FP+FN）", abs(cc.iou - 3 / 7) < 1e-12, f"实际 {cc.iou:.9f}")

    no_pred = gacc.confusion_from_masks(np.zeros((4, 4), bool), np.ones((4, 4), bool))
    check("全不提取时 precision/recall/f1/iou 全为 0（不除零、不返回 nan）",
          (no_pred.precision, no_pred.recall, no_pred.f1, no_pred.iou) == (0.0, 0.0, 0.0, 0.0))
    check("全部漏检时 FN=16、TN=0，且不返回 nan",
          (no_pred.false_negative, no_pred.true_negative, no_pred.recall) == (16, 0, 0.0))

    # 背景占绝大多数：TN=399。若把 TN 放进分母，总体精度会是 399/400=99.75%，
    # 而 IoU 必须仍然是 0（那一个真水像元一个都没提到）。
    sparse_pred = np.zeros((20, 20), bool)
    sparse_truth = np.zeros((20, 20), bool)
    sparse_truth[0, 0] = True
    sparse = gacc.confusion_from_masks(sparse_pred, sparse_truth)
    check("TN 不进入 IoU：TN=399 的稀疏场景 IoU 仍是 0，总体精度式的虚高不会出现",
          sparse.true_negative == 399 and sparse.iou == 0.0,
          f"TN={sparse.true_negative}, IoU={sparse.iou}")

    expect_value_error(
        "形状不一致抛 ValueError（3x3 vs 4x4，不静默 reshape）",
        lambda: gacc.confusion_from_masks(np.zeros((3, 4), bool), np.zeros((4, 3), bool)),
    )

    print()
    print("=" * 74)
    print("步骤 6 / 反向与边界用例")
    print("=" * 74)

    check("match_points([], []) 返回空列表", gacc.match_points([], [], radius_km=0.15) == [])

    empty_rep = gacc.evaluate_points([], [], radius_km=0.15)
    print(f"  空输入报告：n_truth={empty_rep.n_truth} recall={empty_rep.point_recall} "
          f"notes={len(empty_rep.notes)} 条")
    check("evaluate_points([], []) 不抛异常，比率为 0",
          empty_rep.n_truth == 0 and empty_rep.point_recall == 0.0 and empty_rep.coverage_rate == 0.0)
    check("空输入报告带说明（不是悄悄返回一个 0 分）",
          any("没有真值点位" in n for n in empty_rep.notes))

    expect_value_error(
        "radius_km=0 抛 ValueError（半径不能藏默认值，也不能为 0）",
        lambda: gacc.match_points(truth1, [lake_100m], radius_km=0.0),
    )
    expect_value_error(
        "scene_bounds 顺序颠倒抛 ValueError",
        lambda: gacc.evaluate_points(truth1, [], radius_km=0.15, scene_bounds=(78.0, 36.0, 77.0, 35.0)),
    )
    expect_value_error(
        "真值为空多边形抛 ValueError（没有真值不能报 IoU=0）",
        lambda: gacc.iou_of_polygons(a, "POLYGON EMPTY"),
    )
    expect_value_error(
        "非法 WKT 抛 ValueError",
        lambda: gacc.iou_of_polygons("NOT WKT", a),
    )
    empty_pred_iou = gacc.iou_of_polygons("POLYGON EMPTY", a)
    check("预测为空且真值非空时 IoU = 0（确实没提出东西）", empty_pred_iou == 0.0)

    print()
    print("=" * 74)
    print("步骤 7 / 分层约束：accuracy 不得拉进 rasterio / Qt")
    print("=" * 74)

    probe = (
        "import sys\n"
        "import glof.core.accuracy\n"
        "bad = sorted(m for m in ('rasterio', 'PySide6', 'PyQt5', 'PyQt6') if m in sys.modules)\n"
        "print(','.join(bad))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True
    )
    loaded = proc.stdout.strip()
    check(
        "全新进程里 import glof.core.accuracy 不加载 rasterio / Qt",
        proc.returncode == 0 and loaded == "",
        f"模块含：{loaded or '无'}" if proc.returncode == 0 else proc.stderr.strip()[-300:],
    )

    print()
    print("=" * 74)
    total = len(FAILURES)
    checks = CHECKS[0]
    if FAILURES:
        print(f"失败 {total}/{checks} 项：{', '.join(FAILURES)}")
        return 1
    print(f"全部通过（{checks} 项断言）。口径：点位召回分母 = 被影像覆盖的点位数；"
          f"面积在 WGS84 椭球上算。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
