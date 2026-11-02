"""地图模块冒烟：真实小景 + 真实提取 + MapView.qml 在 offscreen 下端到端验证。

运行（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\smoke_ui_map.py

任何一条断言失败都以非零码退出；全部通过打印 ``MAP SMOKE PASS``。
截图证据输出到 ``output/ui_smoke/map_fit.png`` 与 ``map_zoom.png``。

关于提取配方的诚实说明（实测数据，不是猜测）：

* 字面配方 ``extract_lakes(threshold=0.15, min_area_km2=0.005)`` 在克亚吉尔
  2025-09-30 小景上提取 **0** 条记录——整景 NDWI>0.15 的像元只有 1 个
  （雪盖场景、湖水浑浊）。冒烟会先按字面参数跑一遍并把事实打出来。
* 为不空转，回退到本项目管线自证过的配方（``docs/选题诊断与修正建议.md``
  的标定结论 + cli.py 的冰雪掩膜口径）：Otsu 阈值 + ``masks.snow_mask``
  剔雪，可提出 80 条记录（含最大 2.827 km² 的连片水体）。
  注意该景里克亚吉尔湖本体在景界外约 1.7 km（名义坐标换算像素列=-169），
  这些记录是"core 从这景真实提取出的结果"，冒烟用它们驱动 UI、验证
  地图模块的几何/交互/配色，不声称它们是经验证的湖泊编目。
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

# ---- 环境硬规则：必须在 import PySide6 之前设置 ----
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import shiboken6
from PySide6.QtCore import Property, QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QSignalSpy

import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape as shp_shape

from glof.core import io as gio
from glof.core import masks
from glof.core.indices import BAND_PRESETS, ndsi, ndwi
from glof.core.water import extract_lakes, otsu_threshold
from glof.ui.image_provider import RasterImageProvider
from glof.ui.map_controller import MapController
from glof.ui.style import risk_color

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TIF = ROOT / "data" / "kyagar" / "S2A_MSIL2A_20250930T054251_R005_T43SGV_20250930T083759.tif"
OUT_DIR = ROOT / "output" / "ui_smoke"

# 冒烟窗口。640×480 时 fitZoom = max(640/285, 480/906) ≈ 2.246，
# 而第 2 大湖的包围盒能给出 ~3.6 的目标倍率——zoom 变化断言有区分度。
WIN_W, WIN_H = 640, 480

_checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录并打印一条断言。返回布尔值方便短路。"""
    _checks.append((name, bool(ok), detail))
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"   {detail}"
    print(line)
    return bool(ok)


def fail_fast(msg: str) -> int:
    print(f"MAP SMOKE FAIL: {msg}", file=sys.stderr)
    return 1


class _AppShell(QObject):
    """把 MapController 以 ``app.map`` 的上下文属性路径暴露给 QML。

    与主窗口的集成契约一致：QML 只认 ``app.map``，不关心壳是谁。
    """

    def __init__(self, map_controller: QObject, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._map_controller = map_controller

    def _get_map(self) -> QObject:
        return self._map_controller

    map = Property("QVariant", _get_map, constant=True)  # noqa: N815 (QML 命名)


def grab(win: QObject) -> object:
    """QQuickWindow.grabWindow()。rootObjects 返回的包装可能是 QWindow 类型，需换绑。"""
    if hasattr(win, "grabWindow"):
        return win.grabWindow()
    return shiboken6.wrapInstance(shiboken6.getCppPointer(win)[0], QQuickWindow).grabWindow()


WRAPPER_QML = """import QtQuick
import QtQuick.Controls
import "../../glof/ui/qml"

ApplicationWindow {
    width: %d
    height: %d
    visible: true
    color: "#111418"
    MapView { anchors.fill: parent }
}
""" % (WIN_W, WIN_H)

HELLO_QML = b"""import QtQuick
import QtQuick.Controls
ApplicationWindow {
    width: 200; height: 100; visible: true
    Text { anchors.centerIn: parent; text: "hello" }
}
"""


def main() -> int:
    if not TIF.exists():
        return fail_fast(f"测试影像不存在: {TIF}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    app = QGuiApplication([])
    # 尽量与真实运行一致（glof/ui/__main__.py 用 Fusion）。QQuickStyle 需要
    # QApplication，这里只有 QGuiApplication，失败就保持原生样式——
    # 本脚本断言的是地图/表格组件的几何与联动，不依赖控件外观。
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtQuickControls2 import QQuickStyle
        if QApplication.instance() is not None:
            QQuickStyle.setStyle("Fusion")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 阶段 1：最小 hello QML，验证 QML 工具链本身可用
    # ------------------------------------------------------------------
    print("== 阶段 1 / QML 工具链 ==")
    engine0 = QQmlApplicationEngine()
    warnings0: list[str] = []
    engine0.warnings.connect(lambda ws: warnings0.extend(str(w.toString()) for w in ws))
    engine0.loadData(HELLO_QML, QUrl("qrc:/smoke/hello.qml"))
    if not check("hello QML 加载出窗口", len(engine0.rootObjects()) == 1,
                 f"warnings={warnings0[:2]}"):
        return fail_fast("QML 工具链不可用，后续阶段无意义")
    del engine0

    # ------------------------------------------------------------------
    # 阶段 2：真实提取 → MapController → MapView.qml
    # ------------------------------------------------------------------
    print("== 阶段 2 / 提取与控制器 ==")
    t0 = time.perf_counter()
    scene = gio.load_scene(str(TIF), BAND_PRESETS["s2_l2a_6band"])
    ndwi_arr = ndwi(scene["green"], scene["nir"])

    # 1) 先按任务契约的字面参数跑，把事实打出来
    records = extract_lakes(ndwi_arr, scene.transform, scene.crs,
                            threshold=0.15, min_area_km2=0.005)
    print(f"  [事实] 字面配方 extract_lakes(0.15, min 0.005) → {len(records)} 条记录")
    if not records:
        # 2) 回退：本项目标定过的配方（Otsu + NDSI/NIR 双条件雪掩膜）
        snow = masks.snow_mask(ndsi(scene["green"], scene["swir1"]), scene["nir"], 0.4)
        thr = otsu_threshold(ndwi_arr)
        records = extract_lakes(ndwi_arr, scene.transform, scene.crs,
                                threshold=thr, exclude=snow, min_area_km2=0.005)
        print(f"  [回退] Otsu({thr:.4f}) + snow_mask → {len(records)} 条记录")
    t_ext = time.perf_counter() - t0
    if not records:
        return fail_fast("两种配方都提取不到记录，冒烟无法继续")
    print(f"  提取耗时 {t_ext * 1000:.0f} ms（load+extract）")

    # 手工赋等级验证配色：面积前 3 的湖分别 HIGH / MEDIUM / LOW，其余保持 UNKNOWN
    for rec, level in zip(records[:3], ("HIGH", "MEDIUM", "LOW")):
        rec.risk_level = level

    provider = RasterImageProvider()
    controller = MapController(provider)
    controller.set_scene(scene)
    controller.set_lakes(records)

    h_px, w_px = scene.shape
    check("imageWidth == 285", controller.property("imageWidth") == 285,
          f"实际 {controller.property('imageWidth')}")
    check("imageHeight == 906", controller.property("imageHeight") == 906,
          f"实际 {controller.property('imageHeight')}")
    check("Q_PROPERTY imageUrl 是 raster provider 键",
          str(controller.property("imageUrl")).startswith("image://raster/full?v="),
          str(controller.property("imageUrl")))

    polys = controller.property("polygons")
    check("polygons 数 == 湖数", len(polys) == len(records),
          f"{len(polys)} == {len(records)}")
    check("所有 rings 非空（闭合外环 ≥4 点）",
          all(len(p["rings"]) >= 4 for p in polys),
          f"最小环点数 {min(len(p['rings']) for p in polys)}")
    check("颜色全部来自 style.risk_color",
          all(p["color"] == risk_color(p["level"]) for p in polys),
          "HIGH/MEDIUM/LOW/UNKNOWN 各取对应色")
    check("手工等级已进入多边形数据",
          {p["level"] for p in polys[:3]} == {"HIGH", "MEDIUM", "LOW"},
          f"前 3 湖等级 {[p['level'] for p in polys[:3]]}")

    # ------------------------------------------------------------------
    # 阶段 3：加载 MapView.qml（包一层 ApplicationWindow 以便 grabWindow）
    # ------------------------------------------------------------------
    print("== 阶段 3 / MapView.qml 端到端 ==")
    wrapper_path = OUT_DIR / "_smoke_window.qml"
    wrapper_path.write_text(WRAPPER_QML, encoding="utf-8")

    engine = QQmlApplicationEngine()
    qml_warnings: list[str] = []
    engine.warnings.connect(lambda ws: qml_warnings.extend(str(w.toString()) for w in ws))
    engine.addImageProvider(RasterImageProvider.PROVIDER_ID, provider)
    # 壳必须持有引用：临时对象会被 Python GC 回收，QML 侧 app 就成 null 了
    app_shell = _AppShell(controller)
    engine.rootContext().setContextProperty("app", app_shell)
    engine.load(QUrl.fromLocalFile(str(wrapper_path)))
    if not engine.rootObjects():
        for w in qml_warnings:
            print("   ", w)
        return fail_fast("MapView.qml 包装窗口加载失败")
    win = engine.rootObjects()[0]

    image_item = win.findChild(QObject, "mapImage")
    if image_item is None:
        return fail_fast("找不到 mapImage（MapView.qml 结构变了？）")
    # Image.status 是 QQuickImageBase::Status 枚举，PySide6 转不了 Python，
    # 改读 MapView 暴露的 imageReady（QML 侧已包成 bool）
    mv = win.findChild(QObject, "mapView")
    flick = win.findChild(QObject, "mapFlick")
    if mv is None or flick is None:
        return fail_fast("找不到 mapView / mapFlick")
    deadline = time.time() + 10
    while not mv.property("imageReady") and time.time() < deadline:
        app.processEvents()
    check("QML Image 从 provider 加载就绪",
          bool(mv.property("imageReady")),
          f"imageReady={mv.property('imageReady')}")

    repeater = win.findChild(QObject, "polygonsRepeater")
    check("QML Repeater 消费了多边形模型",
          repeater is not None and repeater.property("count") == len(records),
          f"count={repeater.property('count') if repeater is not None else None} vs {len(records)}")

    fit_zoom = float(mv.property("fitZoom"))
    check("初始 zoom == fitZoom（初始 fit）",
          abs(float(mv.property("zoom")) - fit_zoom) < 1e-6,
          f"zoom={float(mv.property('zoom')):.4f} fitZoom={fit_zoom:.4f}")

    app.processEvents()
    fit_shot = grab(win)
    fit_path = OUT_DIR / "map_fit.png"
    fit_shot.save(str(fit_path))
    check("fit 截图已保存且非空", fit_path.exists() and fit_path.stat().st_size > 20_000,
          f"{fit_path.relative_to(ROOT)} {fit_path.stat().st_size} bytes")

    # ---- zoomTo：最大湖横贯整个景宽，zoom 数学上不可能超过 fitZoom 下限
    #（zw ≤ 视口宽/景宽 == fitZoom），所以 zoom 变化用第 2 大湖（独立团块）验证，
    # 同时对最大湖断言 zoomTo 后仍停在合法下限。----
    bbox_big = controller.lake_bbox_px(records[0].lake_id)
    check("lake_bbox_px(最大湖) 返回包围盒", bbox_big is not None, str(bbox_big))
    zoom_before = float(mv.property("zoom"))
    mv.zoomTo(*bbox_big)
    app.processEvents()
    zoom_big = float(mv.property("zoom"))
    check("zoomTo(最大湖) 后 zoom 停在 clamp 下限（合法不变）",
          abs(zoom_big - max(fit_zoom, min(4.0, zoom_before))) < 1e-6,
          f"{zoom_before:.4f} -> {zoom_big:.4f}")

    bbox_2nd = controller.lake_bbox_px(records[1].lake_id)
    mv.zoomTo(*bbox_2nd)
    app.processEvents()
    zoom_2nd = float(mv.property("zoom"))
    check("zoomTo(第2大湖) 后 zoom 变化且在 [fitZoom, 4.0]",
          zoom_2nd != zoom_before and fit_zoom - 1e-6 <= zoom_2nd <= 4.0,
          f"{zoom_before:.4f} -> {zoom_2nd:.4f} (fitZoom={fit_zoom:.4f})")

    zoom_shot = grab(win)
    zoom_path = OUT_DIR / "map_zoom.png"
    zoom_shot.save(str(zoom_path))
    check("zoom 截图已保存", zoom_path.exists() and zoom_path.stat().st_size > 5_000,
          f"{zoom_path.relative_to(ROOT)} {zoom_path.stat().st_size} bytes")

    # ---- zoomAt 锚点不变量：以视口中心为锚放大 1.25x，同一影像像素仍在视口中心 ----
    mv.fit()
    app.processEvents()
    zoom0 = float(mv.property("zoom"))
    cx0, cy0 = float(flick.property("contentX")), float(flick.property("contentY"))
    fw, fh = float(flick.property("width")), float(flick.property("height"))
    ax, ay = (cx0 + fw / 2) / zoom0, (cy0 + fh / 2) / zoom0  # 视口中心的影像像素
    mv.zoomAt(1.25, ax, ay)
    app.processEvents()
    zoom1 = float(mv.property("zoom"))
    ax1 = (float(flick.property("contentX")) + fw / 2) / zoom1
    ay1 = (float(flick.property("contentY")) + fh / 2) / zoom1
    check("zoomAt 放大后 zoom 增大", zoom1 > zoom0,
          f"{zoom0:.4f} -> {zoom1:.4f}")
    check("zoomAt 以光标为锚（锚点视口位置不动）",
          abs(ax1 - ax) < 0.5 and abs(ay1 - ay) < 0.5,
          f"锚偏移 ({abs(ax1 - ax):.3f}, {abs(ay1 - ay):.3f}) px")
    mv.zoomAt(0.5, ax, ay)
    app.processEvents()
    check("zoomAt 缩小被 clamp 到 fitZoom",
          abs(float(mv.property("zoom")) - fit_zoom) < 1e-6,
          f"zoom={float(mv.property('zoom')):.4f}")

    # ---- 点选：QSignalSpy 收 lakeClicked ----
    print("== 阶段 4 / 交互 ==")
    spy_click = QSignalSpy(controller.lakeClicked)

    to_utm = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_user_input(scene.crs),
                                  always_xy=True)
    to_wgs84 = Transformer.from_crs(CRS.from_user_input(scene.crs), CRS.from_epsg(4326),
                                    always_xy=True)
    geoms = [shp_shape(json.loads(r.geometry_wgs84_json)) for r in records]

    big = records[0]
    big_geom = geoms[0]
    rep = big_geom.representative_point()  # 保证在几何内部
    rx, ry = to_utm.transform(rep.x, rep.y)
    rpx, rpy = ~scene.transform * (rx, ry)
    ipx, ipy = int(round(rpx)), int(round(rpy))
    # 四舍五入后的整数像素必须仍落在湖内（大湖 2.8 km²，不成立就是 bug）
    lx, ly = to_wgs84.transform(*[float(v) for v in scene.transform * (ipx, ipy)])
    check("点选测试点确实落在最大湖内", big_geom.intersects(Point(lx, ly)),
          f"({ipx},{ipy})")
    controller.selectAt(ipx, ipy)
    check("selectAt(最大湖内点) → 收到正确 lake_id",
          spy_click.count() == 1 and int(spy_click.at(0)[0]) == big.lake_id,
          f"spy={spy_click.at(0) if spy_click.count() else None} 期望 {big.lake_id}")

    # 点空白：从一组候选里找一个确实不在任何提取几何内的像素（独立于控制器验证）
    def covered(px: int, py: int) -> bool:
        xx, yy = scene.transform * (float(px), float(py))
        lon, lat = to_wgs84.transform(xx, yy)
        return any(g.intersects(Point(lon, lat)) for g in geoms)

    candidates = [(2, 2), (w_px - 3, 2), (2, h_px - 3), (w_px - 3, h_px - 3),
                  (w_px // 2, 2), (w_px // 2, h_px - 3)]
    blank = next(((x, y) for x, y in candidates if not covered(x, y)), None)
    if not check("找到空白像素", blank is not None, f"候选 {candidates}"):
        return fail_fast("影像角落全被多边形覆盖，无法测空白点选")
    spy_click = QSignalSpy(controller.lakeClicked)
    controller.selectAt(*blank)
    check("selectAt(空白) → -1",
          spy_click.count() == 1 and int(spy_click.at(0)[0]) == -1,
          f"spy={spy_click.at(0) if spy_click.count() else None}")

    # 悬停：图内 → 经纬度落在景四角范围内；图外 → NaN
    spy_hover = QSignalSpy(controller.mouseMoved)
    controller.hoverAt(w_px / 2.0, h_px / 2.0)
    lons, lats = [], []
    for cx, cy in ((0, 0), (w_px, 0), (0, h_px), (w_px, h_px)):
        xx, yy = scene.transform * (float(cx), float(cy))
        lon, lat = to_wgs84.transform(xx, yy)
        lons.append(lon)
        lats.append(lat)
    ok_hover = spy_hover.count() == 1
    lon0, lat0 = (spy_hover.at(0)[0], spy_hover.at(0)[1]) if ok_hover else (math.nan, math.nan)
    check("hoverAt(图内) → 经纬度在景范围内",
          ok_hover and min(lons) <= lon0 <= max(lons) and min(lats) <= lat0 <= max(lats),
          f"({lon0:.5f}, {lat0:.5f}) 景 lon[{min(lons):.4f},{max(lons):.4f}] "
          f"lat[{min(lats):.4f},{max(lats):.4f}]")
    controller.hoverAt(-1.0, -1.0)
    controller.hoverAt(float(w_px + 1), float(h_px / 2))
    ok_out = spy_hover.count() == 3 and all(
        math.isnan(spy_hover.at(i)[0]) and math.isnan(spy_hover.at(i)[1])
        for i in (1, 2)
    )
    check("hoverAt(图外) → NaN", ok_out,
          f"spy={ [list(spy_hover.at(i)) for i in range(spy_hover.count())] }")

    check("QML 全程零警告", len(qml_warnings) == 0,
          f"{qml_warnings[:3]}" if qml_warnings else "")

    # ---- 判定 ----
    n_pass = sum(1 for _, ok, _ in _checks if ok)
    print(f"\n共 {len(_checks)} 项断言，通过 {n_pass} 项")
    if n_pass == len(_checks):
        print("MAP SMOKE PASS")
        return 0
    for name, ok, detail in _checks:
        if not ok:
            print(f"  FAIL: {name}   {detail}")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
