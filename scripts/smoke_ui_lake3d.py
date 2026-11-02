"""冒烟：3D 湖面回放（offscreen，DEM 用合成数据，不联网）。

运行（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\smoke_ui_lake3d.py

验证的是**面积→水位这条换算链**，以及 3D 视图对"水位未知"的诚实处理。

为什么不联网：真实 DEM 要走 Planetary Computer，冒烟必须离线可重复。
这里用一个人造碗形 DEM（中心低、四周高），它的水位—面积曲线是解析已知的，
可以逐点核对换算结果。真实 DEM 的标定另见 ``docs/3D回放说明.md``。

关键断言：
  1. 碗形 DEM 上，面积随水位单调增加；反查水位能还原出原面积。
  2. 超出曲线范围的面积返回 None（不是夹到端点）。
  3. 面积 0 / 负数 / NaN 返回 None。
  4. 3D 控制器：地形网格形状正确、帧数与期数一致、当前帧是最后一期。
  5. 水位未知的期保留在序列里，levelKnown=False，relativeLevel 是 NaN。
  6. QML 层：页签存在、滑动条能切帧、水位未知时文案不是 "0"。
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from rasterio.transform import Affine  # noqa: E402

from glof.core.lake_3d import (  # noqa: E402
    build_level_area_curve,
    pixel_area_km2,
)
from glof.ui.app_state import AppState  # noqa: E402
from glof.ui.image_provider import RasterImageProvider  # noqa: E402
from glof.ui.lake3d_controller import Lake3DController, build_terrain_grid  # noqa: E402

QML_DIR = ROOT / "glof" / "ui" / "qml"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _checks.append((name, bool(ok), detail))
    try:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""),
              flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        line = f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else "")
        print(line.encode(enc, "replace").decode(enc), flush=True)
    return bool(ok)


def pump(app: QApplication, seconds: float = 0.15) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def make_bowl(n: int = 120, depth_m: float = 40.0) -> np.ndarray:
    """人造碗形 DEM：中心最低，半径越大越高。水位—面积可解析核对。"""
    y, x = np.mgrid[0:n, 0:n]
    cy = cx = (n - 1) / 2.0
    r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / (n / 2.0)
    return depth_m * (r ** 2)


def main() -> int:
    # ---- 1) 曲线本身：合成碗形 DEM ----
    print("== 步骤 1 / 水位—面积曲线（合成碗形 DEM）==", flush=True)
    n = 120
    dem = make_bowl(n)
    # 0.001° 像元，纬度 35.7
    transform = Affine(0.001, 0.0, 77.0, 0.0, -0.001, 35.8)
    seed = (n // 2, n // 2)
    px = pixel_area_km2(transform, 35.7)
    check("像元面积为正", px > 0, f"{px:.8f} km²")

    curve = build_level_area_curve(dem, transform, seed, lat_mid=35.7,
                                   step_m=1.0, max_rise_m=40.0, radius_px=n // 2)
    check("曲线有效", curve.valid, f"{len(curve.levels)} 个采样点")
    check("面积单调不减",
          all(curve.areas[i] <= curve.areas[i + 1] + 1e-9
              for i in range(len(curve.areas) - 1)),
          f"首 {curve.areas[0]:.4f} 末 {curve.areas[-1]:.4f}")
    check("起点面积接近 0", curve.areas[0] < px * 2, f"{curve.areas[0]:.6f}")

    # 解析核对：碗形 r^2 深度，水位 dz 处半径 r = R*sqrt(dz/depth)，
    # 面积 = pi*r^2。取曲线中间一个点比对（边缘像素化，容差 15%）。
    mid_idx = len(curve.levels) // 2
    dz = curve.levels[mid_idx] - curve.levels[0]
    r_frac = math.sqrt(dz / 40.0)
    analytic = math.pi * (r_frac * n / 2.0) ** 2 * px
    got = curve.areas[mid_idx]
    rel_err = abs(got - analytic) / analytic if analytic > 0 else 1.0
    check("曲线面积与解析解一致（容差 15%）", rel_err < 0.15,
          f"实测 {got:.4f} vs 解析 {analytic:.4f}（相对误差 {rel_err*100:.1f}%）")

    # ---- 2) 反查：面积 -> 水位 -> 面积 ----
    print("== 步骤 2 / 面积反查水位 ==", flush=True)
    for target in (curve.areas[10], curve.areas[20], curve.areas[-1]):
        z = curve.level_for_area(target)
        back = None
        if z is not None:
            # 用曲线自身反查回去：在 levels 里插值 areas
            for i in range(1, len(curve.levels)):
                if curve.levels[i] >= z:
                    z0, z1 = curve.levels[i - 1], curve.levels[i]
                    a0, a1 = curve.areas[i - 1], curve.areas[i]
                    t = 0.0 if z1 <= z0 else (z - z0) / (z1 - z0)
                    back = a0 + t * (a1 - a0)
                    break
        ok = back is not None and abs(back - target) <= max(1e-6, target * 0.02)
        check(f"面积 {target:.4f} 反查后能还原", ok,
              f"水位 {z:.3f} -> 面积 {back:.4f}" if z is not None else "返回 None")

    # ---- 3) 越界与非法输入 ----
    print("== 步骤 3 / 越界与非法输入 ==", flush=True)
    over = curve.areas[-1] * 1.5
    check("超出曲线最大面积返回 None", curve.level_for_area(over) is None, f"{over:.4f}")
    check("面积为 0 返回 None", curve.level_for_area(0.0) is None)
    check("面积为负返回 None", curve.level_for_area(-1.0) is None)
    check("面积为 NaN 返回 None", curve.level_for_area(float("nan")) is None)
    check("相对水位在 0..1 之间",
          all(0.0 <= (curve.fraction_for_area(a) or 0.0) <= 1.0
              for a in curve.areas),
          f"最大 {curve.fraction_for_area(curve.areas[-1]):.3f}")

    # ---- 4) 地形网格 ----
    print("== 步骤 4 / 地形网格 ==", flush=True)
    grid = build_terrain_grid(dem, max_vertices=48)
    check("网格行数 <= 48", grid["rows"] <= 48, f"{grid['rows']}x{grid['cols']}")
    check("高度数 = 行×列", len(grid["heights"]) == grid["rows"] * grid["cols"],
          f"{len(grid['heights'])} vs {grid['rows']*grid['cols']}")
    check("zMin/zMax 与原始 DEM 一致",
          abs(grid["zMin"] - float(np.min(dem))) < 1e-6
          and abs(grid["zMax"] - float(np.max(dem))) < 1e-6,
          f"[{grid['zMin']:.2f}, {grid['zMax']:.2f}]")
    check("合成 DEM 无 NaN", grid["nanCount"] == 0, f"{grid['nanCount']}")

    # NaN 填充：把一块设成 NaN，应被填成最小值且如实计数
    dem_nan = dem.copy()
    dem_nan[0:5, 0:5] = np.nan
    grid_nan = build_terrain_grid(dem_nan, max_vertices=48)
    check("NaN 顶点数被如实报出", grid_nan["nanCount"] > 0, f"{grid_nan['nanCount']}")
    check("NaN 被填成最小值（高度列表无 NaN）",
          all(math.isfinite(h) for h in grid_nan["heights"]))

    # ---- 5) 控制器 ----
    print("== 步骤 5 / Lake3DController ==", flush=True)
    ctrl = Lake3DController()
    check("初始未就绪", not ctrl.property("ready"))
    ctrl.set_terrain(grid)
    check("装载地形后就绪", ctrl.property("ready"))
    check("rows/cols 透传", ctrl.property("rows") == grid["rows"],
          f"{ctrl.property('rows')}")

    dates = ["2018-06-02", "2018-07-02", "2018-08-06", "2018-08-11", "2018-08-26"]
    areas = [curve.areas[3], curve.areas[15], curve.areas[-1],
             curve.areas[8], curve.areas[-1] * 2.0]  # 最后一期故意超范围
    ctrl.set_frames(dates, areas, curve)
    frames = ctrl.property("frames")
    check("帧数与期数一致", len(frames) == len(dates), f"{len(frames)}")
    check("当前帧是最后一期", ctrl.property("currentIndex") == len(dates) - 1,
          f"{ctrl.property('currentIndex')}")
    check("超范围期 levelKnown=False", frames[-1]["levelKnown"] is False,
          f"area={frames[-1]['areaKm2']:.4f}")
    check("超范围期 relativeLevel 是 NaN",
          math.isnan(float(frames[-1]["relativeLevel"])),
          f"{frames[-1]['relativeLevel']}")
    check("超范围期仍在序列里（不跳期）", len(frames) == 5)
    check("正常期 levelKnown=True",
          all(f["levelKnown"] for f in frames[:-1]))
    check("有期次未知时 message 非空", ctrl.property("message") != "",
          str(ctrl.property("message"))[:60])
    check("levelSource 非空（出处可查）", ctrl.property("levelSource") != "")

    ctrl.setCurrentIndex(0)
    check("setCurrentIndex(0) 生效", ctrl.property("currentIndex") == 0)
    ctrl.setCurrentIndex(999)
    check("越界索引被夹到末尾", ctrl.property("currentIndex") == 4)
    ctrl.setCurrentIndex(-5)
    check("负索引被夹到 0", ctrl.property("currentIndex") == 0)

    ctrl.clear()
    check("clear 后未就绪且无帧",
          not ctrl.property("ready") and len(ctrl.property("frames")) == 0)

    # ---- 6) QML 层 ----
    print("== 步骤 6 / QML 页签与时间轴 ==", flush=True)
    app = QApplication.instance() or QApplication([])
    QQuickStyle.setStyle("Fusion")
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda ws: warnings.extend(str(w.toString()) for w in ws))
    provider = RasterImageProvider()
    engine.addImageProvider(RasterImageProvider.PROVIDER_ID, provider)
    state = AppState(provider)
    engine.rootContext().setContextProperty("app", state)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    if not engine.rootObjects():
        print("LAKE3D SMOKE FAIL: Main.qml 加载失败", file=sys.stderr)
        for w in warnings:
            print("QML:", w, file=sys.stderr)
        return 1
    win = engine.rootObjects()[0]
    pump(app, 0.3)

    def find(name):
        return win.findChild(QObject, name)

    check("3D 页签存在", find("tabLake3D") is not None)
    check("3D 页面存在", find("lake3dPage") is not None)
    check("3D 视图存在", find("lake3dView") is not None)
    check("初始未就绪（没检索时序）",
          not bool(state.property("lake3d").property("ready")))

    # 先切到 3D 页签。StackLayout 里非活动页的 visible=false，
    # 里面的控件虽然实例化了，但 visible 为 false——出处的可见性依赖它。
    bottom = find("bottomBar")
    if bottom is not None:
        bottom.setProperty("currentIndex", 2)
        pump(app, 0.2)
    check("切到 3D 页签", bottom is not None and int(bottom.property("currentIndex")) == 2)

    # 直接灌入控制器状态（模拟 DEM 已取回）
    st3d = state.property("lake3d")
    st3d.set_terrain(grid)
    st3d.set_frames(dates, areas, curve)
    pump(app, 0.3)

    slider = find("lake3dSlider")
    check("时间轴滑动条存在", slider is not None)
    if slider is not None:
        check("滑动条上界 = 帧数-1",
              float(slider.property("to")) == len(dates) - 1,
              f"to={slider.property('to')}")

    area_label = find("lake3dAreaLabel")
    level_label = find("lake3dLevelLabel")
    check("面积标签存在", area_label is not None)
    check("水位标签存在", level_label is not None)

    # 切到超范围那一期：水位文案必须是"未知"，不能是 0
    st3d.setCurrentIndex(len(dates) - 1)
    pump(app, 0.2)
    level_text = str(level_label.property("text")) if level_label is not None else ""
    check("超范围期水位文案写「未知」", "未知" in level_text, level_text)
    check("超范围期水位文案不含「0.0%」", "0.0%" not in level_text, level_text)
    area_text = str(area_label.property("text")) if area_label is not None else ""
    check("超范围期面积照常显示", "km²" in area_text, area_text)

    # 切回正常期：水位文案应带百分比
    st3d.setCurrentIndex(0)
    pump(app, 0.2)
    level_text = str(level_label.property("text")) if level_label is not None else ""
    check("正常期水位文案含百分比", "%" in level_text, level_text)

    # 出处标签的可见性依赖 3D 页处于活动状态；页面隐藏时 Text 的绑定
    # 不会求值（实测 text 为空字符串）。所以先确认页签确实切过来了。
    src_label = find("lake3dSourceLabel")
    page_visible = bool(find("lake3dPage").property("visible")) if find("lake3dPage") else False
    check("3D 页处于活动状态", page_visible)
    src_text = str(src_label.property("text")) if src_label is not None else ""
    check("出处标签存在且非空",
          src_label is not None and "DEM" in src_text,
          src_text[:50] if src_text else "(空)")

    check("QML 全程零警告", not warnings, f"{warnings[:3]}" if warnings else "")

    n_pass = sum(1 for _, ok, _ in _checks if ok)
    print(f"\n共 {len(_checks)} 项断言，通过 {n_pass} 项", flush=True)
    if n_pass == len(_checks):
        print("LAKE3D SMOKE PASS", flush=True)
        return 0
    for name, ok, detail in _checks:
        if not ok:
            print(f"  FAIL: {name}   {detail}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
