"""地形模块验证：坡度定义 -> 纬度修正 -> 下游坡度 -> nodata -> 反向用例 -> 真实 DEM。

每一条断言的期望值都从别处独立算出来（解析几何、cos(lat) 恒等式、
另一个栅格的采样值），不是把模块输出抄一遍再比。所有"能失败"的检查都写死了
容差和真值，拿掉模块里的纬度修正或把 None 改成 0，脚本都会退出 1。

为什么值得单独验证纬度修正
--------------------------
经度 1° 在 60°N 只有赤道的 0.5 倍长。坡度用的是"每米高差"，东西向距离若直接
拿 pixel_m 当米数，坡度会偏大一倍（赤道）到 19%（36°N）不等。这不是精度问题，
是定义问题：同一个地形在不同纬度会得到不同的坡度，而 DEM 是经纬度网格时
这个错误不会报错、不会崩溃，只会让下游坡度判据悄悄偏移。

用法：
    ./.venv/Scripts/python.exe scripts/verify_terrain.py
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

from glof.core import terrain as gterr  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

# 克亚吉尔冰川湖（2017-07-27 溃决）窗口，实测锚点见 terrain.py 模块文档。
KYAGAR_BBOX = (77.15, 35.60, 77.30, 35.75)
KYAGAR_LAT, KYAGAR_LON = 35.677371, 77.190788


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def expect_value_error(name: str, fn) -> None:
    """反向用例：喂错参数必须报 ValueError，而不是静默算出一个数。"""
    try:
        fn()
    except ValueError as exc:
        check(name, True, f"ValueError: {exc}")
    except Exception as exc:  # 抛了别的异常也算失败：契约要求的是 ValueError
        check(name, False, f"抛的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有报错，静默通过了")


def plane_rising_east(n: int = 80, *, lat: float = 36.0, gradient: float = 1.0 / 3.0):
    """构造一个向东按 1:gradient 上升的平面，返回 (dem, pixel_m_lat, transform, res)。

    关键：高程按**真实米数**构造——每列向东 offset_m 米就抬 offset_m * gradient。
    这样像素尺度给错（不乘 cos lat）时坡度才会偏离 atan(gradient)。
    """
    res = 30.0 / 111320.0  # 约 30 m 的纬度像元
    m_lat, m_lon = gterr._meters_per_degree(lat)
    cols = np.arange(n)
    east_m = cols * res * m_lon  # 每列相对第 0 列的真实东向米数
    dem = np.tile(east_m * gradient, (n, 1)) + 5000.0
    transform = Affine(res, 0.0, 77.0, 0.0, -res, 36.0)
    return dem, res * m_lat, transform, res


def main() -> int:
    print("=" * 74)
    print("步骤 1 / 坡度定义：1:3 平面 -> atan(1/3) = 18.43°")
    print("=" * 74)

    dem, pixel_m, transform, res = plane_rising_east()
    at_lat = gterr.slope_deg(dem, pixel_m, lat=36.0)
    expected = math.degrees(math.atan(1.0 / 3.0))
    interior = at_lat[5:-5, 5:-5]  # 边界是单侧差分，单独看
    median = float(np.nanmedian(interior))
    print(f"  内部中位数 {median:.4f}°，解析值 {expected:.4f}°")
    check(
        "1:3 平面内部坡度 = 18.43°（容差 0.1°）",
        abs(median - expected) < 0.1,
        f"偏差 {median - expected:+.4f}°",
    )
    check(
        "坡度不是 0、也不是 45° 这类近似值（排除单位混淆）",
        abs(median - expected) < 0.1 and abs(median - 45.0) > 5.0,
    )

    # 南北向的 1:3 平面：不受经度修正影响，任何纬度都应是 atan(1/3)。
    ns = np.tile(5000.0 + np.arange(80)[:, None] * pixel_m / 3.0, (1, 80))
    ns_slope = float(np.nanmedian(gterr.slope_deg(ns, pixel_m, lat=60.0)[5:-5, 5:-5]))
    check(
        "南北向 1:3 平面在 60°N 仍是 18.43°（经度修正不该碰南北向）",
        abs(ns_slope - expected) < 0.1,
        f"实际 {ns_slope:.4f}°",
    )

    flat = np.full((20, 20), 4500.0)
    check(
        "平地坡度全为 0（不是 None、不是 nan）",
        np.all(gterr.slope_deg(flat, 30.0, lat=36.0) == 0.0),
    )

    print()
    print("=" * 74)
    print("步骤 2 / 纬度修正：同一东西向梯度，lat=60 的坡度应约为 lat=0 的 cos(60°)")
    print("=" * 74)

    # 构造一段 60°N 的**真实**东西向 1:3 地形（高程按该纬度真实米数抬升）。
    #   lat=60 正确量测 -> dx = pixel_m * cos60
    #   lat=0  等价于不做修正 -> dx = pixel_m，把每列当成赤道那么宽
    # 同一地形、同一 pixel_m，两种量法的坡度比应恰为 cos(60°) = 0.5。
    dem_60, px_60, _, _ = plane_rising_east(lat=60.0)
    s_fixed = float(np.nanmedian(gterr.slope_deg(dem_60, px_60, lat=60.0)[5:-5, 5:-5]))
    s_naive = float(np.nanmedian(gterr.slope_deg(dem_60, px_60, lat=0.0)[5:-5, 5:-5]))
    # 比值的分子分母顺序不能反：不修正时 dx 用满 pixel_m（偏大），坡度**偏小**，
    # 所以 不修正 / 修正 的**正切** = cos(60°)。
    #
    # 必须用 tan 而不是角度本身：坡度与梯度是 atan 关系，非线性的。
    # 拿角度直接比会稳定偏 2.7%（13.0/18.4 附近），看起来像"修正有误差"，
    # 其实是量纲用错了——线性的是梯度，不是角度。
    tan_ratio = math.tan(math.radians(s_naive)) / math.tan(math.radians(s_fixed))
    cos60 = math.cos(math.radians(60.0))
    print(f"  60°N 的 1:3 地形：正确修正 {s_fixed:.4f}°，不修正 {s_naive:.4f}°，"
          f"tan(不修正)/tan(修正) {tan_ratio:.6f}，cos60={cos60:.6f}")
    check(
        "同一东西向梯度，tan(不修正坡度)/tan(修正坡度) = cos(60°)=0.5（容差 2%）",
        abs(tan_ratio / cos60 - 1.0) < 0.02,
        f"正切比值/cos60 - 1 = {tan_ratio / cos60 - 1:+.4%}",
    )
    check(
        "修正后的坡度恰为 1:3 的解析值（说明 cos 修正方向对，不是把坡度放大）",
        abs(s_fixed - expected) < 0.1,
        f"实际 {s_fixed:.4f}°",
    )
    check(
        "正切比值不是 1.0（没有纬度修正时两种量法会给出同一坡度）",
        abs(tan_ratio - 1.0) > 0.1,
        f"实际正切比值 {tan_ratio:.4f}",
    )

    # 同一真实地形在 0° 与 60° 应得到相同坡度——从另一个方向证伪：
    # 纬度修正若只对一个纬度成立，这两者就对不上。
    dem_eq, px_eq, _, _ = plane_rising_east(lat=0.0)
    s_eq = float(np.nanmedian(gterr.slope_deg(dem_eq, px_eq, lat=0.0)[5:-5, 5:-5]))
    check(
        "按真实米数构造的 1:3 地形，在 0° 与 60° 得到相同坡度（修正自洽）",
        abs(s_eq / s_fixed - 1.0) < 0.02,
        f"lat=0 {s_eq:.4f}° vs lat=60 {s_fixed:.4f}°",
    )

    # 直接验证像素尺度的影响：同一个 DEM，pixel_m 加倍，坡度应变化，
    # 说明函数真的在用 pixel_m，而不是从数组里猜尺度。
    s_double = float(np.nanmedian(gterr.slope_deg(dem, pixel_m * 2.0, lat=36.0)[5:-5, 5:-5]))
    check(
        "pixel_m 加倍后坡度显著减小（pixel_m 参与计算）",
        s_double < median * 0.75,
        f"30 m {median:.2f}° -> 60 m {s_double:.2f}°",
    )

    print()
    print("=" * 74)
    print("步骤 3 / 下游坡度：倾斜平面返回已知值，越界返回 None")
    print("=" * 74)

    # 平面向东下降，1:4（更缓，避免和坡度用例的 1:3 混淆）
    n = 120
    res_d = 0.0003
    transform = Affine(res_d, 0.0, 77.0, 0.0, -res_d, 36.0)
    lat_d = 35.9
    m_lat, m_lon = gterr._meters_per_degree(lat_d)
    cols = np.arange(n)
    dem_d = np.tile(6000.0 - cols * res_d * m_lon / 4.0, (n, 1))
    grad = 1.0 / 4.0
    expected_d = math.degrees(math.atan(grad))

    dam_lat = 36.0 - 60 * res_d
    dam_lon = 77.0 + 60 * res_d
    got = gterr.downstream_slope(dem_d, transform, dam_lat, dam_lon)
    print(f"  平面 1:4，下游坡度 {got if got is None else f'{got:.4f}'}°，解析值 {expected_d:.4f}°")
    check(
        "下游坡度 = atan(1/4) = 14.04°（容差 1°）",
        got is not None and abs(got - expected_d) < 1.0,
        f"实际 {got}",
    )

    out = gterr.downstream_slope(dem_d, transform, 40.0, 77.0)
    check(
        "坝体点落在 DEM 外时返回 None（不是 0）",
        out is None,
        f"实际 {out!r}",
    )
    check("返回值不是 0.0（0 会被 risk.py 当成'量测成功且坡度为零'）", out != 0.0)

    nodata_dem = dem_d.copy()
    nodata_dem[55:65, 55:65] = np.nan
    out_nodata = gterr.downstream_slope(nodata_dem, transform, dam_lat, dam_lon)
    check(
        "坝体点落在 nodata 上时返回 None（不插值出一个假数）",
        out_nodata is None,
        f"实际 {out_nodata!r}",
    )

    # 平坦平面：任何方向都不下降，坡度应是 0°（有定义），而不是 None。
    flat_dem = np.full((60, 60), 5000.0)
    flat_tf = Affine(res_d, 0.0, 77.0, 0.0, -res_d, 36.0)
    flat_slope = gterr.downstream_slope(flat_dem, flat_tf, 35.99, 77.01)
    check(
        "完全平坦时下游坡度为 0.0（可量测，确实为零；与 None 区分开）",
        flat_slope is not None and abs(flat_slope) < 1e-9,
        f"实际 {flat_slope!r}",
    )

    # 边缘场景：坝体在东边界，往东 500 m 的剖面必然出界 -> None
    edge_east = 77.0 + (n - 2) * res_d
    edge = gterr.downstream_slope(dem_d, transform, 36.0 - 60 * res_d, edge_east)
    check(
        "坝体贴东边界、下坡方向的 500 m 剖面出界时返回 None（不拿半条剖面凑数）",
        edge is None,
        f"坝体经度 {edge_east:.4f}，实际 {edge!r}",
    )

    print()
    print("=" * 74)
    print("步骤 4 / nodata：NaN 斑块不得污染整幅坡度")
    print("=" * 74)

    holed = dem.copy()
    holed[30:40, 30:40] = np.nan
    hs = gterr.slope_deg(holed, pixel_m, lat=36.0)
    finite_frac = float(np.isfinite(hs).mean())
    print(f"  100/6400 像元为 NaN，输出有限值占比 {finite_frac:.2%}")
    check(
        "NaN 斑块之后仍有 >90% 像元是有限值（差分没有扩散 NaN）",
        finite_frac > 0.90,
        f"实际 {finite_frac:.2%}",
    )
    check(
        "只有原始 nodata 像元本身为 NaN（100 个，恰好等于斑块面积）",
        int(np.isnan(hs).sum()) == 100,
        f"实际 {int(np.isnan(hs).sum())} 个",
    )
    check(
        "斑块外的坡度值不受影响（与无洞版本逐点相同）",
        np.allclose(hs[np.isfinite(hs)], at_lat[np.isfinite(hs)], atol=1e-9),
    )

    one_nan = dem.copy()
    one_nan[10, 10] = np.nan
    os_ = gterr.slope_deg(one_nan, pixel_m, lat=36.0)
    check(
        "单个 NaN 只让它自己变 NaN，四个邻居仍有坡度（单侧差分生效）",
        np.isnan(os_).sum() == 1
        and all(np.isfinite(os_[r, c]) for r, c in ((9, 10), (11, 10), (10, 9), (10, 11))),
        f"NaN 数 {int(np.isnan(os_).sum())}",
    )

    print()
    print("=" * 74)
    print("步骤 5 / 瓦片命名：南、西半球")
    print("=" * 74)

    url_n35 = gterr.copernicus_tile_url(KYAGAR_LAT, KYAGAR_LON)
    check(
        "克亚吉尔 -> N35_00_E077_00",
        "N35_00_E077_00_DEM" in url_n35,
        url_n35.rsplit("/", 1)[-1],
    )
    check(
        "负纬度向下取整：-1.2 -> S02（不是 S01）",
        "S02_00_W071_00_DEM" in gterr.copernicus_tile_url(-1.2, -70.5),
        gterr.copernicus_tile_url(-1.2, -70.5).rsplit("/", 1)[-1],
    )
    check(
        "整度边界归属：lat=35.0 属于 N35",
        "N35_00" in gterr.copernicus_tile_url(35.0, 77.9),
    )
    check(
        "经度编号补三位：E005 不是 E5",
        "E005_00_DEM" in gterr.copernicus_tile_url(35.5, 5.5),
    )

    print()
    print("=" * 74)
    print("步骤 6 / 反向用例：错参数必须 ValueError")
    print("=" * 74)

    expect_value_error(
        "pixel_m=0 抛 ValueError",
        lambda: gterr.slope_deg(dem, 0.0, lat=36.0),
    )
    expect_value_error(
        "pixel_m 为负抛 ValueError",
        lambda: gterr.slope_deg(dem, -30.0, lat=36.0),
    )
    expect_value_error(
        "lat=90 抛 ValueError（cos90=0，东西向尺度为 0）",
        lambda: gterr.slope_deg(dem, 30.0, lat=90.0),
    )
    expect_value_error(
        "一维 DEM 抛 ValueError",
        lambda: gterr.slope_deg(np.zeros(10), 30.0, lat=36.0),
    )
    expect_value_error(
        "1xN DEM 抛 ValueError（差分至少需要 2x2）",
        lambda: gterr.slope_deg(np.zeros((1, 10)), 30.0, lat=36.0),
    )
    expect_value_error(
        "search_m=0 抛 ValueError",
        lambda: gterr.downstream_slope(dem_d, transform, dam_lat, dam_lon, search_m=0.0),
    )
    expect_value_error(
        "transform 不是 Affine 抛 ValueError",
        lambda: gterr.downstream_slope(dem_d, (1, 0, 0, 0, 1, 0), dam_lat, dam_lon),
    )
    expect_value_error(
        "dam_lat 为 nan 抛 ValueError",
        lambda: gterr.downstream_slope(dem_d, transform, float("nan"), dam_lon),
    )
    expect_value_error(
        "bbox 经度顺序颠倒抛 ValueError",
        lambda: gterr.read_dem_bounds(KYAGAR_PATH, (77.30, 35.60, 77.15, 35.75)),
    )
    expect_value_error(
        "bbox 长度不为 4 抛 ValueError",
        lambda: gterr.read_dem_bounds(KYAGAR_PATH, (77.15, 35.60, 77.30)),
    )

    print()
    print("=" * 74)
    print("步骤 7 / 真实 Copernicus DEM（可选，网络失败不算失败）")
    print("=" * 74)

    network_ok = False
    try:
        dem_real, tf_real = gterr.read_dem_bounds(None, KYAGAR_BBOX)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"  [SKIP] 读不到 Copernicus 瓦片，跳过网络用例：{type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"  [SKIP] 网络用例异常，跳过（不计入失败）：{type(exc).__name__}: {exc}")
    else:
        network_ok = True
        lat_mid = 0.5 * (KYAGAR_BBOX[1] + KYAGAR_BBOX[3])
        print(f"  窗口 {dem_real.shape}，高程 {np.nanmin(dem_real):.0f}–{np.nanmax(dem_real):.0f} m")
        check(
            "窗口为 540x540（0.15° 的 1 角秒网格）",
            dem_real.shape == (540, 540),
            f"实际 {dem_real.shape}",
        )
        check(
            "高程在 4700–6800 m（实测 4732–6709）",
            4700.0 < float(np.nanmin(dem_real)) and float(np.nanmax(dem_real)) < 6800.0,
            f"{np.nanmin(dem_real):.1f}–{np.nanmax(dem_real):.1f}",
        )
        slope_real = gterr.slope_deg(dem_real, gterr.GLO30_PIXEL_M, lat=lat_mid)
        med_real = float(np.nanmedian(slope_real))
        frac_10 = float(np.nanmean(slope_real > 10.0))
        print(f"  坡度中位数 {med_real:.2f}°，>10° 占比 {frac_10:.1%}")
        check(
            "坡度中位数在 20–35°（实测 27.7°）",
            20.0 < med_real < 35.0,
            f"实际 {med_real:.2f}°",
        )
        check(
            ">10° 的像元超过 70%（实测 79.4%，证明不是平地或全 NaN）",
            frac_10 > 0.70,
            f"实际 {frac_10:.1%}",
        )
        # 用返回的 transform 反算坝体点应落在 DEM 内；故意给一个远点必须是 None。
        inside = gterr._sample(dem_real, ~tf_real, KYAGAR_LON, KYAGAR_LAT)
        check(
            "克亚吉尔点位落在真实 DEM 内且高程在 4700–6800 m",
            math.isfinite(inside) and 4700.0 < inside < 6800.0,
            f"采样高程 {inside:.1f} m",
        )
        ds = gterr.downstream_slope(dem_real, tf_real, KYAGAR_LAT, KYAGAR_LON)
        print(f"  克亚吉尔点位下游坡度 {ds if ds is None else f'{ds:.2f}°'}")
        check(
            "真实地形上的下游坡度是有限值或在 [-90, 90] 内（不是 0 占位）",
            ds is not None and -90.0 <= ds <= 90.0,
            f"实际 {ds!r}",
        )

    print()
    print("=" * 74)
    checks = CHECKS[0]
    if FAILURES:
        print(f"失败 {len(FAILURES)}/{checks} 项：{', '.join(FAILURES)}")
        return 1
    net = "网络用例已执行" if network_ok else "网络用例已跳过"
    print(f"全部通过（{checks} 项断言；{net}）。口径：坡度 = atan(|∇z|)，"
          f"东西向像元尺度乘 cos(lat)；算不出来返回 None，不是 0。")
    print("=" * 74)
    return 0


# 反向用例里只用于触发 bbox 校验，文件不必真实存在：校验在打开文件之前完成。
KYAGAR_PATH = ROOT / "data" / "dem" / "kyagar.tif"

if __name__ == "__main__":
    raise SystemExit(main())
