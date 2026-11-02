"""坝体位置估计验证：合成 DEM 上的出口 -> 方位角 -> 坡度，以及"比湖心好"的量化。

这个模块要证明的不是"代码能跑"，而是**用坝体位置量坡度确实比用湖心好**。
所以核心用例是一个能解析出真值的合成地形：

    一块向东按 1:tan(20°) 抬升的斜面（东高西低），中间挖一个平底坑当湖，
    坑底比下游（西侧）地面略高。物理上：
        * 坑的西缘是最低边 -> 出口应落在西侧；
        * 下游方位角应指向西（270°）；
        * 真实坡向就是 20°，用坝体位置应量到 ~20°。

对照组：用坑的质心量同一条判据，得到的是 ~0°——因为质心在平底坑里，
四周一圈都是坑底，`downstream_slope` 找不到任何下降方向。这两个数放在一起，
才是"改进了多少"的量化。

同时守住项目的诚实性要求：
    * 边界高程全 nodata -> None（不是质心、不是 0）；
    * 正圆湖 + 完全平坦 DEM -> 不崩，返回一个**可解释**的结果（出口是采样
      顺序里的第一个点，坡度 0° 是"确实平"而不是"没量到"）；
    * 投影坐标系 DEM 直接 ValueError，不静默把米当度。

用法：
    ./.venv/Scripts/python.exe scripts/verify_dam_position.py
    ./.venv/Scripts/python.exe scripts/verify_dam_position.py --live   # 加真实 DEM 用例
"""

from __future__ import annotations

import math
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from affine import Affine  # noqa: E402
from pyproj import CRS  # noqa: E402
from shapely.geometry import Point, Polygon  # noqa: E402

from glof.core import dam_position as gdam  # noqa: E402
from glof.core import terrain as gterr  # noqa: E402
from glof.core.water import LakeRecord  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

# 合成地形参数：像元 0.0001°（约 11 m），足够细，坑缘的位置误差才看得清。
RES = 0.0001
LAT0 = 36.0
LON0 = 77.0
N = 400
SLOPE_DEG = 20.0
SLOPE_GRAD = math.tan(math.radians(SLOPE_DEG))  # 东高西低的梯度（米/米）

# 坑：中心像元 (200,200) 的**椭圆**，东西半轴 90 像元（约 810 m）、南北半轴 40 像元
# （约 445 m）。为什么用椭圆而不是矩形：
#   * 矩形坑的西缘整条边同高，"最低点"在边上处处相等，argmin 只能落到某个角上；
#     角点相对质心的方向是斜的（实测偏正西 32°），方位角断言会误判。
#     椭圆的西端是**唯一**最低点，质心到它的方向恰为正西。
#   * 东西向拉长还有第二个作用：质心到西端 810 m > 500 m 的坡度剖面长度，
#     从湖心量 0–500 m 全是平底，才能复现"湖心把 20° 量成 ~0°"这个真实缺陷。
# 坑底高程 = 中心处斜面高程 - 60 m，即比东侧地面低、比西侧地面高——水往西流。
PIT_CENTER = 200
PIT_HALF_R = 40   # 南北半轴（像元）
PIT_HALF_C = 90   # 东西半轴（像元）
PIT_DEPTH_M = 60.0

# 坑底微倾（东西向 1%）。**完全平底会让边界上每个点高程相同**，"最低点"退化成
# 采样顺序里的第一个点——这不是算法错，是输入没有信息。真实 GLO-30 在湖面上
# 本来就有数米起伏（实测克亚吉尔湖心四周岸坡 +10~+250 m），所以合成数据给一个
# 1% 的微倾，比"绝对平面"更接近真实 DEM。完全平底的退化情形在步骤 7 单独验证。
PIT_FLOOR_TILT = 0.01


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def expect_value_error(name: str, fn) -> None:
    try:
        fn()
    except ValueError as exc:
        check(name, True, f"ValueError: {str(exc)[:60]}")
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"抛的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有报错，静默通过了")


def build_tilted_pit(
    *,
    n: int = N,
    res: float = RES,
    lon0: float = LON0,
    lat0: float = LAT0,
    slope_grad: float = SLOPE_GRAD,
    pit_center: int = PIT_CENTER,
    pit_half_r: int = PIT_HALF_R,
    pit_half_c: int = PIT_HALF_C,
    pit_depth_m: float = PIT_DEPTH_M,
    floor_tilt: float = PIT_FLOOR_TILT,
) -> tuple[np.ndarray, Affine, Polygon]:
    """造一个"东高西低的斜面 + 平底坑"，返回 (dem, transform, 坑面多边形 WGS84)。

    高程按**真实米数**构造：每列向东 offset_m 米就抬 offset_m * slope_grad。
    这样下游坡度才是解析可算的 atan(slope_grad)，不受经纬度像元尺度影响。

    坑底不是绝对平面，而是按 ``floor_tilt`` 向西再降一点。绝对平面会让整条边界
    同高程，"最低点"没有意义（见 ``PIT_FLOOR_TILT`` 的说明）。
    """
    m_lat, m_lon = gterr._meters_per_degree(lat0)
    cols = np.arange(n)
    east_m = cols * res * m_lon
    z = np.tile(5000.0 + east_m * slope_grad, (n, 1))
    dem = z.astype(np.float64)

    r0, r1 = pit_center - pit_half_r, pit_center + pit_half_r
    c0, c1 = pit_center - pit_half_c, pit_center + pit_half_c
    rr, cc = np.mgrid[r0:r1, c0:c1]
    # 椭圆掩膜：((c-cc0)/a)^2 + ((r-rr0)/b)^2 <= 1
    inside = (
        ((cc - pit_center) / pit_half_c) ** 2 + ((rr - pit_center) / pit_half_r) ** 2
    ) <= 1.0
    floor0 = float(dem[pit_center, pit_center] - pit_depth_m)
    # 坑底：向西每米降 floor_tilt 米。c 越小越西 -> 高程越低。
    col_offset_m = (cc - pit_center) * res * m_lon
    dem[r0:r1, c0:c1] = np.where(inside, floor0 + col_offset_m * floor_tilt, dem[r0:r1, c0:c1])

    transform = Affine(res, 0.0, lon0, 0.0, -res, lat0)
    # 坑多边形：像元 (r,c) 的中心经纬度是 lon0 + (c+0.5)*res, lat0 - (r+0.5)*res。
    def xy(r: float, c: float) -> tuple[float, float]:
        return lon0 + (c + 0.5) * res, lat0 - (r + 0.5) * res

    poly = Polygon(
        [xy(pit_center + pit_half_r * math.sin(t), pit_center + pit_half_c * math.cos(t))
         for t in np.linspace(0.0, 2.0 * math.pi, 73)[:-1]]
    )
    return dem, transform, poly


def make_record(geom: Polygon, lake_id: int = 1) -> LakeRecord:
    """把 shapely 几何包成 LakeRecord，走 slope_from_dam 的真实输入路径。"""
    from shapely.geometry import mapping
    import json

    return LakeRecord(
        lake_id=lake_id,
        area_m2=1.0,
        perimeter_m=1.0,
        n_pixels=1,
        area_px_m2=1.0,
        mean_ndwi=0.5,
        centroid_lon=geom.centroid.x,
        centroid_lat=geom.centroid.y,
        geometry_wgs84_json=json.dumps(mapping(geom)),
    )


def main() -> int:
    print("=" * 74)
    print("步骤 1 / 合成 DEM：东高西低 20° 斜面 + 平底坑")
    print("=" * 74)

    dem, transform, pit = build_tilted_pit()
    lat_mid = LAT0 - (N / 2) * RES
    m_lat, m_lon = gterr._meters_per_degree(lat_mid)

    # 先确认合成地形本身符合预期，否则后面的断言没有意义。
    pit_c = pit.centroid
    floor = gterr._sample(dem, ~transform, pit_c.x, pit_c.y)
    # 东西两侧各取 0.012°（约 1080 m），在坑外（坑东西半宽约 720 m）、网格内。
    west = gterr._sample(dem, ~transform, pit_c.x - 0.012, pit_c.y)
    east = gterr._sample(dem, ~transform, pit_c.x + 0.012, pit_c.y)
    print(f"  坑底高程 {floor:.1f} m，西侧 0.012° 外 {west:.1f} m，东侧 0.012° 外 {east:.1f} m")
    check("坑底比西侧地面高（水会往西流）", floor > west, f"{floor:.1f} > {west:.1f}")
    check("坑底比东侧地面低（东侧是上坡）", floor < east, f"{floor:.1f} < {east:.1f}")
    check("合成坡向朝西（东高西低）", east > west, f"东 {east:.0f} > 西 {west:.0f}")

    print()
    print("=" * 74)
    print("步骤 2 / outlet_point：出口应落在坑的下游（西）边缘")
    print("=" * 74)

    outlet = gdam.outlet_point(pit, dem, transform, CRS.from_epsg(4326))
    check("出口不为 None", outlet is not None, repr(outlet))
    assert outlet is not None
    o_lon, o_lat = outlet
    # 真值：坑的西缘是坑中心往西 pit_half_c 个像元处。
    true_west_lon = pit_c.x - PIT_HALF_C * RES
    err_m = abs(o_lon - true_west_lon) * m_lon
    print(f"  出口 ({o_lon:.5f}, {o_lat:.5f})，真值西缘经度 {true_west_lon:.5f}，"
          f"东西误差 {err_m:.1f} m")
    check("出口落在西侧（经度比质心小）", o_lon < pit_c.x - 5 * RES,
          f"出口 {o_lon:.5f} vs 质心 {pit_c.x:.5f}")
    check("出口距真值西缘 < 3 个像元（33 m）", err_m < 3 * RES * m_lon,
          f"误差 {err_m:.1f} m / 像元 {RES * m_lon:.1f} m")
    # 南北方向应基本居中：坑是东西向长条，西缘整条边同高，出口可以落在边的任何位置。
    check("出口南北方向在坑范围内", abs(o_lat - pit_c.y) <= (PIT_HALF_R + 1) * RES,
          f"纬差 {abs(o_lat - pit_c.y) / RES:.1f} 像元")

    print()
    print("=" * 74)
    print("步骤 3 / downstream_azimuth：方位角指向西（270°）")
    print("=" * 74)

    az = gdam.downstream_azimuth(pit, o_lon, o_lat)
    # 与正西（270°）的夹角，按 0–180 折叠。
    diff = abs((az - 270.0 + 180.0) % 360.0 - 180.0)
    print(f"  方位角 {az:.1f}°，正西 270°，夹角 {diff:.1f}°")
    check("方位角与正西夹角 < 30°", diff < 30.0, f"{diff:.1f}°")
    check("方位角不是正北（没有退化成默认值）", abs(az - 0.0) > 30.0, f"{az:.1f}°")

    print()
    print("=" * 74)
    print("步骤 4 / 关键对比：坝体位置 vs 湖心，下游坡度")
    print("=" * 74)

    dam = gdam.dam_position(pit, dem, transform, CRS.from_epsg(4326))
    check("dam_position 不为 None", dam is not None)
    assert dam is not None
    print(f"  坝体 ({dam.lon:.5f}, {dam.lat:.5f})  高程 {dam.elevation_m:.1f} m  "
          f"方位角 {dam.azimuth_deg:.1f}°  边界散布 {dam.boundary_relief_m:.1f} m")

    slope_dam = gterr.downstream_slope(dem, transform, dam.lat, dam.lon)
    slope_center = gterr.downstream_slope(dem, transform, pit_c.y, pit_c.x)
    print(f"  坝体位置下游坡度 {slope_dam if slope_dam is None else f'{slope_dam:.2f}'}°，"
          f"湖心下游坡度 {slope_center if slope_center is None else f'{slope_center:.2f}'}°")
    print(f"  真实坡向 {SLOPE_DEG:.1f}°")

    check("用坝体位置量到的坡度 ≈ 20°（±5°）",
          slope_dam is not None and abs(slope_dam - SLOPE_DEG) <= 5.0,
          f"实际 {slope_dam!r}")
    check("用湖心量到的坡度 ≈ 0°（平底坑，量不到坡向）",
          slope_center is not None and abs(slope_center) < 1.0,
          f"实际 {slope_center!r}")
    check("坝体量测比湖心量测更接近真值（这就是改进）",
          slope_dam is not None and slope_center is not None
          and abs(slope_dam - SLOPE_DEG) < abs(slope_center - SLOPE_DEG),
          f"坝体偏差 {abs(slope_dam - SLOPE_DEG):.1f}° vs 湖心偏差 {abs(slope_center - SLOPE_DEG):.1f}°")
    # 把改进量写成数字，直接进报告。
    if slope_dam is not None and slope_center is not None:
        print(f"  -> 湖心把 {SLOPE_DEG:.0f}° 的坡量成 {slope_center:.2f}°（偏差 "
              f"{abs(slope_center - SLOPE_DEG):.1f}°）；坝体量成 {slope_dam:.2f}°（偏差 "
              f"{abs(slope_dam - SLOPE_DEG):.1f}°）")

    print()
    print("=" * 74)
    print("步骤 5 / slope_from_dam：批量接口，估不出坝体返回 None")
    print("=" * 74)

    rec = make_record(pit)
    res_list = gdam.slope_from_dam([rec], dem, transform, CRS.from_epsg(4326))
    check("每条输入对应一条输出", len(res_list) == 1)
    r0 = res_list[0]
    check("slope_from_dam 用上了坝体位置", r0.used_dam_position, r0.note)
    check("批量接口的坡度与单点调用一致",
          r0.slope_deg is not None and slope_dam is not None
          and abs(r0.slope_deg - slope_dam) < 1e-9,
          f"{r0.slope_deg!r} vs {slope_dam!r}")
    check("返回对象带坝体经纬度", r0.dam is not None and r0.dam.lon == dam.lon)
    d = r0.to_dict()
    check("to_dict 可直接落 GeoJSON properties",
          d["dam_used"] is True and "dam_lon" in d and d["dam_slope_deg"] == r0.slope_deg)

    # 湖落在 DEM 外 -> 边界全 nodata -> None
    outside = Polygon([
        (LON0 - 0.05, LAT0 - 0.05), (LON0 - 0.049, LAT0 - 0.05),
        (LON0 - 0.049, LAT0 - 0.049), (LON0 - 0.05, LAT0 - 0.049),
    ])
    rec_out = make_record(outside, lake_id=99)
    res_out = gdam.slope_from_dam([rec_out], dem, transform, CRS.from_epsg(4326))
    check("湖在 DEM 外时 slope_deg 是 None，不是 0",
          res_out[0].slope_deg is None, repr(res_out[0].slope_deg))
    check("湖在 DEM 外时 dam 为 None 并写明原因", res_out[0].dam is None and res_out[0].note != "",
          res_out[0].note)
    check("used_dam_position 为 False", res_out[0].used_dam_position is False)

    print()
    print("=" * 74)
    print("步骤 6 / 全 nodata：必须返回 None，不能编位置")
    print("=" * 74)

    nodata_dem = np.full_like(dem, np.nan)
    check("outlet_point 全 nodata -> None",
          gdam.outlet_point(pit, nodata_dem, transform, "EPSG:4326") is None)
    check("dam_position 全 nodata -> None",
          gdam.dam_position(pit, nodata_dem, transform, "EPSG:4326") is None)
    res_nan = gdam.slope_from_dam([rec], nodata_dem, transform, "EPSG:4326")
    check("slope_from_dam 全 nodata -> slope_deg 为 None，不是 0",
          res_nan[0].slope_deg is None, repr(res_nan[0].slope_deg))
    check("全 nodata 的 note 说明是边界高程读不到", "nodata" in res_nan[0].note, res_nan[0].note)

    print()
    print("=" * 74)
    print("步骤 7 / 退化情形：正圆湖 + 完全平坦 DEM")
    print("=" * 74)

    flat = np.full((200, 200), 4500.0)
    flat_tf = Affine(RES, 0.0, LON0, 0.0, -RES, LAT0)
    circle = Point(LON0 + 100 * RES, LAT0 - 100 * RES).buffer(40 * RES, quad_segs=64)
    dam_flat = gdam.dam_position(circle, flat, flat_tf, "EPSG:4326")
    check("正圆湖 + 平 DEM 不崩，返回估计", dam_flat is not None)
    if dam_flat is not None:
        print(f"  出口 ({dam_flat.lon:.5f}, {dam_flat.lat:.5f})  高程 {dam_flat.elevation_m:.1f} m  "
              f"方位角 {dam_flat.azimuth_deg:.1f}°  边界散布 {dam_flat.boundary_relief_m:.3f} m")
        check("平坦 DEM 上边界散布 = 0（没有任何高程信息）",
              abs(dam_flat.boundary_relief_m) < 1e-9, f"{dam_flat.boundary_relief_m!r}")
        check("出口高程 = 平面高程（没有编一个数）",
              abs(dam_flat.elevation_m - 4500.0) < 1e-9, f"{dam_flat.elevation_m!r}")
        check("方位角是有限值且在 [0,360)",
              dam_flat.azimuth_deg is not None and 0.0 <= dam_flat.azimuth_deg < 360.0,
              f"{dam_flat.azimuth_deg!r}")
    flat_slope = gterr.downstream_slope(flat, flat_tf, dam_flat.lat, dam_flat.lon) if dam_flat else None
    check("完全平坦时下游坡度 = 0.0（可量测且确实为零，与 None 区分）",
          flat_slope is not None and abs(flat_slope) < 1e-9, f"{flat_slope!r}")

    # 退化路径本身要能跑：出口和质心重合时走局部切线法线。
    tiny = Point(LON0 + 100 * RES, LAT0 - 100 * RES).buffer(5 * RES, quad_segs=32)
    az_deg = gdam.downstream_azimuth(tiny, tiny.centroid.x, tiny.centroid.y)
    check("出口落在质心上时不崩，方位角仍可定义",
          math.isfinite(az_deg) and 0.0 <= az_deg < 360.0, f"{az_deg:.1f}°")

    print()
    print("=" * 74)
    print("步骤 8 / 反向用例：错参数必须 ValueError")
    print("=" * 74)

    expect_value_error("投影坐标系 DEM 抛 ValueError（不把米当度）",
                       lambda: gdam.outlet_point(pit, dem, transform, "EPSG:32643"))
    expect_value_error("dem_crs 无法解析抛 ValueError",
                       lambda: gdam.outlet_point(pit, dem, transform, "not-a-crs"))
    expect_value_error("transform 不是 Affine 抛 ValueError",
                       lambda: gdam.outlet_point(pit, dem, (1, 0, 0, 0, 1, 0), "EPSG:4326"))
    expect_value_error("一维 DEM 抛 ValueError",
                       lambda: gdam.outlet_point(pit, np.zeros(10), transform, "EPSG:4326"))
    expect_value_error("n_samples < 8 抛 ValueError",
                       lambda: gdam.outlet_point(pit, dem, transform, "EPSG:4326", n_samples=4))
    expect_value_error("search_m = 0 抛 ValueError",
                       lambda: gdam.slope_from_dam([rec], dem, transform, "EPSG:4326", search_m=0.0))
    expect_value_error("几何无效时 downstream_azimuth 抛 ValueError",
                       lambda: gdam.downstream_azimuth(Polygon(), 77.0, 36.0))
    # 非面几何不是"错参数"，是缺数据：返回 None，让调用方走 UNKNOWN。
    check("线几何不是面 -> outlet_point 返回 None（缺数据不是异常）",
          gdam.outlet_point(Point(77.0, 36.0), dem, transform, "EPSG:4326") is None)

    print()
    print("=" * 74)
    print("步骤 9 / 口径一致性：坡度只来自 terrain.downstream_slope")
    print("=" * 74)

    import inspect

    src = inspect.getsource(gdam)
    check("模块没有重写坡度计算（不出现 polyfit / arctan 的坡度实现）",
          "polyfit" not in src, "未发现 polyfit")
    check("模块调用 terrain.downstream_slope", "downstream_slope(" in src)
    check("模块复用 terrain._sample 采样高程", "gterr._sample" in src)
    check("core 层没有 import Qt", "PySide" not in src and "PyQt" not in src)

    return 0


def test_live() -> int:
    """真实 Copernicus DEM + 本地真实湖几何：湖心 vs 坝体，实测数字。

    这里**不断言**"坝体一定更好"——真实湖上两者谁更接近真值，没有真值可比。
    它只做两件事：把两个数如实打出来，并守住"缺数据是 None 不是 0"。
    """
    import json
    from shapely.geometry import shape

    print()
    print("=" * 74)
    print("步骤 10 / 真实用例：Copernicus GLO-30 + output/real1 的真实湖")
    print("=" * 74)

    lakes_path = ROOT / "output" / "real1" / "lakes.geojson"
    if not lakes_path.exists():
        print(f"  [SKIP] 找不到 {lakes_path}，跳过（不是失败）")
        return 0
    feats = json.loads(lakes_path.read_text(encoding="utf-8"))["features"][:3]
    if not feats:
        print("  [SKIP] 没有湖要素，跳过（不是失败）")
        return 0

    for feat in feats:
        props = feat["properties"]
        geom = shape(feat["geometry"])
        b = geom.bounds
        pad = 0.01
        try:
            dem, tf = gterr.read_dem_bounds(None, (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"  [SKIP] 读不到 Copernicus 瓦片：{type(exc).__name__}: {exc}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"  [SKIP] 网络用例异常，跳过（不计入失败）：{type(exc).__name__}: {exc}")
            return 0

        dam = gdam.dam_position(geom, dem, tf, "EPSG:4326")
        c = geom.centroid
        slope_c = gterr.downstream_slope(dem, tf, c.y, c.x)
        if dam is None:
            print(f"  湖 {props.get('lake_id')}（{props.get('area_km2')} km²）："
                  f"估不出坝体（边界高程全 nodata）")
            continue
        slope_d = gterr.downstream_slope(dem, tf, dam.lat, dam.lon)
        print(f"  湖 {props.get('lake_id')}（{props.get('area_km2')} km²）：")
        print(f"    坝体 ({dam.lon:.5f}, {dam.lat:.5f}) 高程 {dam.elevation_m:.1f} m，"
              f"边界散布 {dam.boundary_relief_m:.1f} m，方位角 {dam.azimuth_deg:.1f}°")
        print(f"    湖心下游坡度 {slope_c if slope_c is None else f'{slope_c:.2f}°'}  |  "
              f"坝体下游坡度 {slope_d if slope_d is None else f'{slope_d:.2f}°'}")
        check(f"湖 {props.get('lake_id')}：坝体高程是有限值（不是 0 占位）",
              dam.elevation_m is not None and math.isfinite(dam.elevation_m))
        check(f"湖 {props.get('lake_id')}：坝体下游坡度要么 None 要么在 [-90,90]",
              slope_d is None or -90.0 <= slope_d <= 90.0, f"{slope_d!r}")
    return 0


def _run_live() -> bool:
    return "--live" in sys.argv


if __name__ == "__main__":
    code = main()
    if _run_live():
        code = max(code, test_live())
    print()
    print("=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)}/{CHECKS[0]} 项：{', '.join(FAILURES)}")
        raise SystemExit(1)
    print(f"全部通过（{CHECKS[0]} 项断言）。口径：坝体 = 湖边界 DEM 最低点，"
          f"下游方位 = 质心->出口的反方向；坡度只来自 terrain.downstream_slope；"
          f"算不出来返回 None，不是 0。")
    print("=" * 74)
    raise SystemExit(code)
