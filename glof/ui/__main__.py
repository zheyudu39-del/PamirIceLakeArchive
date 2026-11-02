"""``python -m glof.ui`` 的入口：QApplication + QQmlApplicationEngine + AppState。

为什么是 QApplication 而不是 QGuiApplication：底部时序曲线是 QtCharts 的
ChartView，它在 C++ 层链接 QtWidgets——QGuiApplication 下 ChartView 一构造就
访问违例（PySide6 6.12 实测，见 scripts/smoke_ui_series.py 的环境硬规则 1）。

--smoke（无头全链路冒烟，全程本地素材、不联网）::

    .venv\\Scripts\\python.exe -m glof.ui --smoke

QT_QPA_PLATFORM / QT_QUICK_BACKEND 必须在 QApplication 与 QML 场景创建之前
生效，所以在 import PySide6 前按 sys.argv 判定设置（项目环境硬规则）。

冒烟序列与诚实性说明：

1. 真实鼠标点击"提取湖泊"按钮（契约参数 threshold=0.15, min_area=0.005）——
   默认影像（T43SGV 2025-09-30 小景）实测提出 **0 条**记录（NDWI 最高约
   0.167，见 scripts/smoke_ui_map.py），这是 core 的真实结果；冒烟把它当
   "0 条也正常走完全链路"来断言，不造假数字。
2. Python 侧再以 threshold=0.05 提取同一景 → ≥1 条记录，驱动地图多边形 /
   表格 / 选中联动的断言。
3. data/spike 的 T43SFV 景（2025-09-28 全湖窗口，契约参数下 200+ 条，见
   scripts/smoke_ui_table.py）做一次"满载"提取，验证大规模多边形 + 表格。
4. "检索时序"按钮以空年份区间（结束年 < 起始年）点击：SeriesWorker 不联网、
   立即返回空序列——离线验证 buildSeries 的 QML→槽→worker→controller 链路。
5. 风险接线（离线）：selectLake 选中当前最大湖 → _apply_series_result 注入
   13 期合成点 → lake_profile 真查本地 ICIMOD 事件库拿坝型/溃决年 →
   assess_series 判面积最大期（2018-08-06 满蓄 1.8254 km²）为 HIGH →
   表格 dataChanged + 地图多边形重着色 + selectedLakeRisk 状态栏摘要回流，
   截图 output/ui_smoke/app_risk.png 存证。
6. grabWindow 截图 output/ui_smoke/app.png，全部断言通过退出码 0。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ---- --smoke 的 Qt 环境变量必须先于 QApplication / QML 场景生效 ----
if "--smoke" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    # offscreen 平台不带字体（Qt 已不随包发布字体），不指路则中文全是豆腐块
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

import shiboken6  # noqa: E402
from PySide6.QtCore import (QCoreApplication, QEvent, QEventLoop, QObject,  # noqa: E402
                            QPoint, QTimer, Qt, QUrl)
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from glof.core.inventory import lake_profile  # noqa: E402
from glof.core.timeseries import SeriesPoint  # noqa: E402
from glof.ui.app_state import DEFAULT_INPUT, AppState  # noqa: E402
from glof.ui.image_provider import RasterImageProvider  # noqa: E402
from glof.ui.texture_provider import TerrainTextureProvider  # noqa: E402
from glof.ui.style import risk_color  # noqa: E402

QML_DIR = Path(__file__).resolve().parent / "qml"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "output" / "ui_smoke"
SPIKE_INPUT = "data/spike/S2C_MSIL2A_20250928T053701_R005_T43SFV_20250928T103015.tif"

# 冒烟注入的 13 期合成点（2018 消融期，形态对齐 kyagar_dense.csv 的实测形状：
# 早期低 → 中期涨到平台 1.8254 附近 → 08-06 满蓄为全序列最大 → 溃决后回落）。
# 08-06 之前有 7 期（≥3），留出法年内回退成立，平台=1.79 → 比值 1.0198 > 0.95。
_SYNTH_2018: list[tuple[str, float]] = [
    ("2018-06-02", 0.35), ("2018-06-12", 0.52), ("2018-06-22", 0.74),
    ("2018-07-02", 0.98), ("2018-07-12", 1.21), ("2018-07-22", 1.55),
    ("2018-07-27", 1.79), ("2018-08-06", 1.8254), ("2018-08-16", 1.42),
    ("2018-08-26", 0.61), ("2018-09-05", 0.43), ("2018-09-12", 0.38),
    ("2018-09-20", 0.31),
]


def _synthetic_series_2018() -> list[SeriesPoint]:
    """合成 13 期 SeriesPoint（全程离线；覆盖质量给满，不触发窄条过滤）。"""
    return [
        SeriesPoint(
            date=date, year=2018, area_km2=area, valid_px=378324, valid_frac=1.0,
            tile="T43SGV", cloud=5.0, scene=f"smoke-synth/{date}",
            ndwi_max=0.45, water_frac=0.31,
        )
        for date, area in _SYNTH_2018
    ]

# engine.warnings 收集到的 QML 警告（冒烟末尾断言为空）
qml_warnings: list[str] = []


def _window(engine: QQmlApplicationEngine) -> QQuickWindow:
    """根窗口换绑成 QQuickWindow：rootObjects 的包装类型不稳，按指针取真身。"""
    win = engine.rootObjects()[0]
    if isinstance(win, QQuickWindow):
        return win
    return shiboken6.wrapInstance(shiboken6.getCppPointer(win)[0], QQuickWindow)


def _grab(engine: QQmlApplicationEngine):
    """根窗口 grabWindow（换绑逻辑同 _window）。"""
    return _window(engine).grabWindow()


# ======================================================================
# --smoke 冒烟
# ======================================================================

_checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _checks.append((name, bool(ok), detail))
    # Windows 控制台默认 GBK，打印 "km²" 的 ²（U+00B2）会抛 UnicodeEncodeError。
    # 断言已经记进 _checks 了，让打印本身把进程带崩毫无道理——退回替换字符。
    try:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""),
              flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        line = f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else "")
        print(line.encode(enc, "replace").decode(enc), flush=True)
    return bool(ok)


def smoke_fail(msg: str) -> int:
    print(f"APP SMOKE FAIL: {msg}", file=sys.stderr)
    return 1


def _click_item(win: QQuickWindow, object_name: str) -> bool:
    """真实鼠标点击窗口里某个具名 Item 的中心（QTest 走事件系统，不经 Python 直调）。"""
    item = win.findChild(QObject, object_name)
    if item is None:
        return False
    center = item.mapToItem(None, item.property("width") / 2.0, item.property("height") / 2.0)
    QTest.mouseClick(win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(int(float(center.x())), int(float(center.y()))))
    return True


def _wait_signal(sig_done, sig_fail, timeout_s: float, app: QApplication):
    """QEventLoop 等 AppState 的完成/失败信号，返回 ("ok"/"failed"/"timeout", 载荷)。

    QEventLoop.exec 处理事件期间，工作线程的排队信号得以送达主线程；
    QTimer 单发兜底，防止 worker 卡死把冒烟挂住。
    """
    outcome: dict = {}
    loop = QEventLoop()

    def on_done(*args):
        outcome["payload"] = args
        outcome["kind"] = "ok"
        loop.quit()

    def on_fail(text):
        outcome["payload"] = text
        outcome["kind"] = "failed"
        loop.quit()

    sig_done.connect(on_done)
    sig_fail.connect(on_fail)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(int(timeout_s * 1000))
    try:
        loop.exec()
    finally:
        timer.stop()
        sig_done.disconnect(on_done)
        sig_fail.disconnect(on_fail)
    return outcome.get("kind", "timeout"), outcome.get("payload")


def run_smoke(app: QApplication, engine: QQmlApplicationEngine, state: AppState,
              input_arg: str) -> int:
    win = _window(engine)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def find(name: str):
        return win.findChild(QObject, name)

    if not check("Main.qml 加载出根窗口", len(engine.rootObjects()) == 1,
                 f"warnings={qml_warnings[:3]}" if qml_warnings else ""):
        return smoke_fail("主窗口加载失败")

    # ---- 1) 点"提取湖泊"按钮：契约参数 (0.15, 0.005)，默认影像 → 0 条（真实结果） ----
    print("== 步骤 1 / 按钮驱动提取（契约参数，预期 0 条）==", flush=True)
    field = find("year1Field")
    if not check("找到参数输入框", field is not None):
        return smoke_fail("QML 结构缺输入框")
    t0 = time.perf_counter()
    if not check("点击 提取湖泊 按钮", _click_item(win, "extractButton")):
        return smoke_fail("找不到提取按钮")
    kind, payload = _wait_signal(state.extractFinished, state.extractFailed, 30.0, app)
    elapsed = time.perf_counter() - t0
    if not check("extracted/failed 在 30s 内到达", kind == "ok", f"kind={kind}"):
        return smoke_fail("提取未完成")
    records = payload[1]
    check("契约参数 (0.15, 0.005) 提取 0 条（该景 NDWI max≈0.167 的真实结果）",
          len(records) == 0, f"{len(records)} 条，耗时 {elapsed:.1f}s")
    check("busyExtract 复位", not state.busyExtract)
    check("lastError 为空", state.lastError == "", state.lastError)
    check("progressText 报告完成", "提取完成" in state.progressText, state.progressText)

    # ---- 2) threshold=0.05 复提同一景 → ≥1 条，驱动地图/表格 ----
    print("== 步骤 2 / threshold=0.05 复提（拿到记录驱动联动）==", flush=True)
    state.extract(input_arg, 0.05, 0.005)
    kind, payload = _wait_signal(state.extractFinished, state.extractFailed, 30.0, app)
    if not check("threshold=0.05 提取完成", kind == "ok", f"kind={kind}"):
        return smoke_fail("复提未完成")
    records = payload[1]
    if not check("拿到 ≥1 条记录", len(records) >= 1, f"{len(records)} 条"):
        return smoke_fail("0.05 也提不出记录，冒烟无法继续")
    rid = int(records[0].lake_id)

    map_ctrl = state.property("map")
    polys = map_ctrl.property("polygons")
    check("地图多边形数 == 记录数", len(polys) == len(records),
          f"{len(polys)} == {len(records)}")
    check("湖泊表行数 == 记录数", state.lakes.rowCount() == len(records),
          f"{state.lakes.rowCount()} == {len(records)}")
    repeater = find("polygonsRepeater")
    check("QML Repeater 消费了多边形", repeater is not None
          and repeater.property("count") == len(records),
          f"count={repeater.property('count') if repeater is not None else None}")

    mv = find("mapView")
    deadline = time.time() + 10
    while mv is not None and not mv.property("imageReady") and time.time() < deadline:
        app.processEvents()
    check("换景后地图影像就绪", mv is not None and bool(mv.property("imageReady")))

    # selectLake → 地图高亮（Python 侧属性断言）
    state.selectLake(rid)
    app.processEvents()
    hl = [p["lakeId"] for p in map_ctrl.property("polygons") if p["highlighted"]]
    check("selectLake 后地图只高亮该湖", hl == [rid], f"{hl} 期望 [{rid}]")

    # app.map.lakeClicked → 表格联动（QML Connections 的装配路径）
    table_item = find("lakeTable")
    if not check("找到 LakeTable 实例", table_item is not None):
        return smoke_fail("QML 结构缺 lakeTable")
    map_ctrl.lakeClicked.emit(rid)
    app.processEvents()
    check("map.lakeClicked → table.highlightedId 跟随",
          int(table_item.property("highlightedId")) == rid,
          f"highlightedId={table_item.property('highlightedId')} 期望 {rid}")
    row = state.lakes.row_of(rid)
    check("map.lakeClicked → table.selectRow 选中该行",
          int(table_item.property("selectedRow")) == row,
          f"selectedRow={table_item.property('selectedRow')} 期望 {row}")
    map_ctrl.lakeClicked.emit(-1)
    app.processEvents()
    check("map.lakeClicked(-1) → 取消表格选中",
          int(table_item.property("highlightedId")) == -1,
          f"highlightedId={table_item.property('highlightedId')}")

    # 表格 → 地图：真实点击某行，地图多边形高亮应切到该行（selectRow 只做
    # 程序化选中不发信号，所以必须模拟真实点击才能踩到这条装配路径）。
    # LakeTable 在 StackLayout 的第 2 页，非活动页 visible=false 收不到鼠标
    # 事件——先真实点击"湖泊列表"页签切过去。
    target = records[-1] if len(records) > 1 else records[0]
    if not check("点击 湖泊列表 页签切页", _click_item(win, "tabTable")):
        return smoke_fail("找不到页签")
    app.processEvents()
    table_item.selectRow(int(target.lake_id))
    app.processEvents()
    p = table_item.delegateCenter(state.lakes.row_of(int(target.lake_id)), 0)
    ok_pos = p is not None and float(p.x()) > 0
    check("表格行 delegate 可点（有窗口坐标）", ok_pos,
          f"中心=({float(p.x()):.0f},{float(p.y()):.0f})" if ok_pos else "delegate 未实例化")
    if ok_pos:
        QTest.mouseClick(win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(int(float(p.x())), int(float(p.y()))))
        app.processEvents()
        hl = [q["lakeId"] for q in map_ctrl.property("polygons") if q["highlighted"]]
        check("点击表格行 → 地图高亮切到该湖", hl == [int(target.lake_id)],
              f"{hl} 期望 [{target.lake_id}]")

    # 悬停 → 状态栏经纬度；图外 → "—"
    coord = find("statusCoord")
    h_px, w_px = map_ctrl.property("imageHeight"), map_ctrl.property("imageWidth")
    map_ctrl.hoverAt(float(w_px) / 2.0, float(h_px) / 2.0)
    app.processEvents()
    in_text = str(coord.property("text")) if coord is not None else ""
    check("悬停图内 → 状态栏显示经纬度", "°E" in in_text and "°N" in in_text, in_text)
    map_ctrl.hoverAt(-1.0, -1.0)
    app.processEvents()
    out_text = str(coord.property("text")) if coord is not None else ""
    check("悬停图外 → 状态栏显示 —", out_text.endswith("—"), out_text)

    # ---- 3) 满载：data/spike T43SFV 景（契约参数 200+ 条，本地素材） ----
    spike = PROJECT_ROOT / SPIKE_INPUT
    if spike.is_file():
        print("== 步骤 3 / 满载提取（data/spike T43SFV，契约参数）==", flush=True)
        t0 = time.perf_counter()
        state.extract(str(spike), 0.15, 0.005)
        kind, payload = _wait_signal(state.extractFinished, state.extractFailed,
                                     120.0, app)
        elapsed = time.perf_counter() - t0
        if not check("满载提取完成", kind == "ok", f"kind={kind}"):
            return smoke_fail("满载提取失败")
        spike_records = payload[1]
        check("满载记录 ≥1", len(spike_records) >= 1,
              f"{len(spike_records)} 条，耗时 {elapsed:.1f}s")
        check("满载后表格行数同步", state.lakes.rowCount() == len(spike_records),
              f"{state.lakes.rowCount()} == {len(spike_records)}")
    else:
        print("== 步骤 3 / 跳过：data/spike 素材不存在 ==", flush=True)

    # ---- 4) 检索时序按钮：空年份区间（离线）验证 QML→槽→worker 链路 ----
    print("== 步骤 4 / 检索时序按钮（空年份区间，不联网）==", flush=True)
    y0f, y1f = find("year0Field"), find("year1Field")
    y0f.setProperty("text", "2016")
    y1f.setProperty("text", "2015")  # 结束年 < 起始年 → years=[] → SeriesWorker 不联网
    if not check("点击 检索时序 按钮", _click_item(win, "seriesButton")):
        return smoke_fail("找不到时序按钮")
    kind, payload = _wait_signal(state.seriesFinished, state.seriesFailed, 30.0, app)
    check("seriesFinished 在 30s 内到达（空年份立即返回）", kind == "ok", f"kind={kind}")
    if kind == "ok":
        check("序列为空且无错误", len(payload[0]) == 0 and state.lastError == "",
              f"{len(payload[0])} 期, lastError={state.lastError!r}")
        check("busySeries 复位", not state.busySeries)
        chart = find("seriesChart")
        check("SeriesChart 回到空态", chart is not None
              and int(chart.property("shownPoints")) == 0
              and not bool(chart.property("hasData")))
    y1f.setProperty("text", "2025")  # 恢复默认年份，截图状态与面板默认一致
    app.processEvents()

    # ---- 5) 风险接线：选中最大湖 → 注入 13 期合成点 → 判定回流（离线） ----
    # 库是本地 zip（data/raw），lake_profile 真查询；时序点是合成值，不走网络。
    print("== 步骤 5 / 风险接线（选中湖 → 事件库 → 逐期判定 → 表格/地图/状态栏）==",
          flush=True)
    current = map_ctrl.lake_records
    if not check("当前有可选中记录", len(current) >= 1, f"{len(current)} 条"):
        return smoke_fail("无记录，风险接线冒烟无法继续")
    # 最大湖：探针实测 spike 满载最大湖即克亚吉尔本湖（5.5688 km²，质心距库内
    # Kyagar 点 3.7 km，落在 0.05° 邻域内）→ lake_profile 命中冰坝系 34 条事件。
    target = max(current, key=lambda r: r.area_m2)
    state.selectLake(int(target.lake_id))
    app.processEvents()
    check("selectLake 记住选中湖",
          int(state.property("selectedLakeId")) == int(target.lake_id),
          f"selectedLakeId={state.property('selectedLakeId')} 期望 {target.lake_id}")
    check("selectedLakeLabel 摘要非空（id/面积/质心）",
          "#" in str(state.property("selectedLakeLabel"))
          and "km²" in str(state.property("selectedLakeLabel")),
          str(state.property("selectedLakeLabel")))

    # 选中湖质心 → 真查本地事件库（与 _apply_series_result 同一调用）
    prof = lake_profile(target.centroid_lat, target.centroid_lon)
    check("lake_profile 真查询本地库命中（profile 非 None）", prof is not None,
          f"{prof!r}" if prof else "None")
    if prof is not None:
        check("选中湖坝型属冰坝系（库值含 ice）", "ice" in prof.lake_type.lower(),
              f"lake_type={prof.lake_type!r}, n_events={prof.n_events}")
        check("2018 ∈ 库事件年", 2018 in prof.event_years,
              f"{prof.first_year}–{prof.last_year} 共 {len(prof.event_years)} 年")

    spy_data = QSignalSpy(state.lakes.dataChanged)
    check("QSignalSpy 挂上 dataChanged", spy_data.isValid())
    state._apply_series_result(_synthetic_series_2018())  # 冒烟注入口（与 worker 回调同一路径）
    deadline = time.time() + 5
    while spy_data.count() == 0 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    check("dataChanged 有发出（QSignalSpy 记录，轮询等待）",
          spy_data.count() >= 1, f"count={spy_data.count()}")

    check("序列 13 期上曲线",
          len(state.property("series").property("points")) == 13,
          f"{len(state.property('series').property('points'))} 期")
    check("事件年来自库查询（2018 ∈ series.eventYears）",
          2018 in list(state.property("series").property("eventYears")),
          f"eventYears={list(state.property('series').property('eventYears'))}")
    check("平台 = 满蓄期 1.8254",
          abs(float(state.property("series").property("plateau")) - 1.8254) < 1e-3,
          f"plateau={state.property('series').property('plateau')}")

    row = state.lakes.row_of(int(target.lake_id))
    check("选中湖 risk_level 更新为 HIGH（面积最大期 2018-08-06 满蓄判定）",
          target.risk_level == "HIGH", f"risk_level={target.risk_level!r}")
    check("risk_reasons 非空且含 [命中]",
          len(target.risk_reasons) > 0
          and any("[命中]" in r for r in target.risk_reasons),
          f"{len(target.risk_reasons)} 条，首条 {target.risk_reasons[0][:40]}…"
          if target.risk_reasons else "空")
    idx_lvl = state.lakes.index(row, 6)
    check("表格风险列单元格显示 HIGH（data() 路径）",
          state.lakes.data(idx_lvl) == "HIGH"
          and state.lakes.data(idx_lvl, state.lakes.ROLE_COLOR) == risk_color("HIGH"),
          f"display={state.lakes.data(idx_lvl)!r}")
    poly = next((p for p in map_ctrl.property("polygons")
                 if p["lakeId"] == int(target.lake_id)), None)
    check("地图多边形重着色为 HIGH 红（update_lake_risk 路径）",
          poly is not None and poly["color"] == risk_color("HIGH")
          and poly["level"] == "HIGH",
          f"color={poly['color'] if poly else None}")
    check("selectedLakeRisk 摘要非空且含 HIGH",
          "HIGH" in str(state.property("selectedLakeRisk")),
          str(state.property("selectedLakeRisk")))

    # 风险截图拍"面积时序"页：13 期曲线 + 平台线 + 事件年参考线，
    # 与步骤 6 的"湖泊列表"页互补。原先这里也拍表格页，两张图字节完全相同
    # （同状态同画面），作为证据等于只有一张。
    check("切到 面积时序 页签（风险截图状态）", _click_item(win, "tabSeries"))
    for _ in range(8):
        app.processEvents()
    risk_png = OUT_DIR / "app_risk.png"
    img_risk = _grab(engine)
    saved_risk = (not img_risk.isNull()) and img_risk.save(str(risk_png))
    size_risk = risk_png.stat().st_size if risk_png.exists() else 0
    check("app_risk.png 已保存且非空", saved_risk and size_risk > 30_000,
          f"{size_risk} bytes")
    chart = find("seriesChart")
    check("风险截图页显示 13 期曲线", chart is not None
          and int(chart.property("shownPoints")) == 13,
          f"shownPoints={chart.property('shownPoints') if chart is not None else None}")
    status_risk = find("statusRisk")
    check("状态栏显示选中湖风险摘要",
          status_risk is not None and bool(status_risk.property("visible"))
          and "HIGH" in str(status_risk.property("text")),
          str(status_risk.property("text")) if status_risk is not None else "找不到 statusRisk")

    # ---- 6) 截图存证 ----
    print("== 步骤 6 / 截图 ==", flush=True)
    # 切回"湖泊列表"页：截图里能看到地图多边形 + 满载表格 + 判据详情
    check("切到 湖泊列表 页签（截图状态）", _click_item(win, "tabTable"))
    for _ in range(8):
        app.processEvents()
    png = OUT_DIR / "app.png"
    img = _grab(engine)
    saved = (not img.isNull()) and img.save(str(png))
    size = png.stat().st_size if png.exists() else 0
    check("app.png 已保存且非空", saved and size > 30_000, f"{size} bytes")
    check("QML 全程零警告", not qml_warnings,
          f"{qml_warnings[:3]}" if qml_warnings else "")

    n_pass = sum(1 for _, ok, _ in _checks if ok)
    print(f"\n共 {len(_checks)} 项断言，通过 {n_pass} 项", flush=True)
    if n_pass == len(_checks):
        print("APP SMOKE PASS", flush=True)
        return 0
    for name, ok, detail in _checks:
        if not ok:
            print(f"  FAIL: {name}   {detail}", flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m glof.ui", description="帕米尔冰湖档案主窗口")
    parser.add_argument("--input", default=DEFAULT_INPUT,
                        help="初始影像路径（填进输入框，默认契约素材）")
    parser.add_argument("--smoke", action="store_true",
                        help="offscreen 无头冒烟：提取→联动→截图，退出码 0 为通过")
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv)

    # QML 控件样式必须显式设成 Fusion。
    # Windows 原生样式（windowsvista）**不支持定制 background/contentItem**，
    # 会给每个 Button/TextField 打 "The current style does not support
    # customization of this control"，界面保持系统灰、自绘样式全部失效。
    # Fusion 是 Qt 自带的跨平台样式，支持完整定制，且各平台表现一致。
    # 必须在建 QQmlApplicationEngine **之前**设置。
    QQuickStyle.setStyle("Fusion")

    engine = QQmlApplicationEngine()
    engine.warnings.connect(
        lambda ws: qml_warnings.extend(str(w.toString()) for w in ws))

    provider = RasterImageProvider()
    engine.addImageProvider(RasterImageProvider.PROVIDER_ID, provider)

    # 3D 地形贴图提供器。软件渲染后端不消费 ProceduralMesh 的 normals/colors
    # 顶点属性（实测有法线和没法线逐像素一致），山体明暗改走贴图。
    terrain_texture = TerrainTextureProvider()
    engine.addImageProvider(TerrainTextureProvider.PROVIDER_ID, terrain_texture)

    state = AppState(provider, default_input=args.input,
                     terrain_texture=terrain_texture)
    engine.rootContext().setContextProperty("app", state)

    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    if not engine.rootObjects():
        for w in qml_warnings:
            print("QML:", w, file=sys.stderr)
        return 1

    if args.smoke:
        code = run_smoke(app, engine, state, args.input)
        _teardown(app, engine)
        return code

    code = app.exec()
    _teardown(app, engine)
    return code


def _teardown(app: QApplication, engine: QQmlApplicationEngine) -> None:
    """确定性拆树：显式冲刷 DeferredDelete，让引擎先于 AppState 消亡。

    解释器退出时 Python 对象回收顺序不定：若 AppState 的 C++ 对象先于引擎
    被 GC，上下文属性失效会触发 QML 绑定重估，打一串
    "Cannot read property 'map' of null" 的 TypeError 噪音（offscreen 实测
    20 条）。普通 processEvents() 不投递 DeferredDelete 事件，必须
    sendPostedEvents 显式冲一遍（实测噪音归零）。
    """
    for obj in engine.rootObjects():
        obj.deleteLater()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
