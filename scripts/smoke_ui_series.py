"""冒烟：时序曲线 + 后台线程（offscreen，全程不联网）。

对应任务书五步：QtCharts 探针 -> CSV 造点与断言 -> ExtractWorker 实提取
-> SeriesWorker 空年份 -> grabWindow 截图。全部真跑，断言数字真实打印。

--------------------------------------------------------------------------
本机实测出的两条 Qt 环境硬规则（本次冒烟最先验证的就是它们）
--------------------------------------------------------------------------
1. QtCharts 的 QML 插件必须配 **QApplication**（QtWidgets）。用
   QGuiApplication 时 ChartView 一构造就访问违例崩溃（退出码 0xC0000005），
   原因是 QtCharts 在 C++ 层链接 QtWidgets；offscreen / offscreen+software
   两种情形都崩，与渲染后端无关。所以本脚本用 QApplication。
2. PySide6 不再自带字体，offscreen 下轴标签的中文画不出来；
   设 QT_QPA_FONTDIR 指到系统字体目录后正常。

--------------------------------------------------------------------------
关于 Canvas 退化路径
--------------------------------------------------------------------------
任务书允许"QtCharts 不可用则退化为 Canvas 手绘折线"。实测探针通过
（规则 1 就是解法），所以 SeriesChart.qml 只实现了 QtCharts 一条路径，
不养跑不到的死代码；若探针失败本脚本直接带原因退出（exit 2），由
汇报写明，不假装降级成功。

运行（不联网）：

    .venv\\Scripts\\python.exe scripts\\smoke_ui_series.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- Qt 环境变量必须在 import PySide6 之前设置 ----
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import Property, QObject, QUrl  # noqa: E402
from PySide6.QtQuick import QQuickView  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import shiboken6  # noqa: E402

from glof.core.timeseries import SeriesPoint, estimate_plateau  # noqa: E402
from glof.ui.series_controller import SeriesController  # noqa: E402
from glof.ui.worker import ExtractWorker, SeriesWorker  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCENE_TIF = ROOT / "data" / "kyagar" / "S2A_MSIL2A_20250930T054251_R005_T43SGV_20250930T083759.tif"
DENSE_CSV = ROOT / "output" / "kyagar" / "kyagar_dense.csv"
SERIES_QML = ROOT / "glof" / "ui" / "qml" / "SeriesChart.qml"
OUT_DIR = ROOT / "output" / "ui_smoke"

# 克亚吉尔湖 AOI（WGS84），与 scripts/kyagar_series.py 同一窗口量级。
# years=[] 时 build_series 根本不会走到检索，随便给个合法 bbox 即可，无网络。
AOI_BBOX = (75.05, 36.90, 75.25, 37.10)

MINIMAL_QML = """import QtQuick
import QtCharts
ChartView {
    width: 320; height: 240
    animationOptions: ChartView.NoAnimation
    LineSeries {
        axisX: ValuesAxis { min: 0; max: 1 }
        axisY: ValuesAxis { titleText: "km2" }
        XYPoint { x: 0; y: 1 }
        XYPoint { x: 1; y: 3 }
    }
}
"""


class _AppShell(QObject):
    """主窗口装配前的最小 app 外壳：QML 只认 app.series 这一条路径。"""

    def __init__(self, series: SeriesController) -> None:
        super().__init__()
        self._series = series

    @Property(QObject, constant=True)
    def series(self) -> SeriesController:  # noqa: N802 - QML 侧属性名固定
        return self._series


def spy_dump(spy: QSignalSpy) -> list[list]:
    """QSignalSpy 在 PySide6 不可迭代，统一转成普通嵌套 list 方便打印。"""
    return [list(spy.at(i)) for i in range(spy.count())]


def wait_for_signal(spy: QSignalSpy, timeout_s: float, app: QApplication) -> bool:
    """等 spy 记录到信号，返回是否等到。

    不用 ``QSignalSpy.wait()``：实测（PySide6 6.12.0，Windows）它阻塞期间
    不放 GIL，工作线程的 run() 拿不到解释器，一个信号都发不出来——
    表现为等满超时、spy 全空、线程还挂着（QThread: Destroyed while
    thread is still running）。改用 processEvents + sleep 轮询：
    sleep 每轮都把 GIL 让给工作线程，QSignalSpy 只负责记录，各司其职。
    """
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        app.processEvents()
        if spy.count() > 0:
            return True
        time.sleep(0.01)
    return spy.count() > 0


def load_dense_points(csv_path: Path) -> list[SeriesPoint]:
    """kyagar_dense.csv -> SeriesPoint 列表（valid_frac 取 CSV 值、water_frac=nan）。

    说明：任务书描述的列名里有 ndwi_max，实际 CSV 表头是
    tile,cloud,valid_px,valid_frac,area_km2,water_frac,scene,date,year——
    没有 ndwi_max 列，按 NaN 处理（不影响本冒烟的任何断言）。
    """
    points: list[SeriesPoint] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            points.append(
                SeriesPoint(
                    date=row["date"],
                    year=int(row["year"]),
                    area_km2=float(row["area_km2"]),
                    valid_px=int(row["valid_px"]),
                    valid_frac=float(row["valid_frac"]),
                    tile=row["tile"],
                    cloud=float(row["cloud"]),
                    scene=row["scene"],
                    ndwi_max=float("nan"),
                    water_frac=float("nan"),
                )
            )
    return points


def probe_qtcharts(app: QApplication) -> float:
    """步骤 1：最小 QML 验证 QtCharts 在 offscreen+software 下可加载。

    返回加载+抓帧耗时（秒）。失败时打印全部 QML 错误并 exit(2)。
    """
    t0 = time.perf_counter()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    probe_qml = OUT_DIR / "_probe_qtcharts.qml"
    probe_qml.write_text(MINIMAL_QML, encoding="utf-8")
    try:
        view = QQuickView()
        view.setSource(QUrl.fromLocalFile(str(probe_qml)))
        errors = [e.toString() for e in view.errors()]
        ready = view.status() == QQuickView.Status.Ready and not errors
        if ready:
            view.show()
            app.processEvents()
            img = view.grabWindow()
            ready = not img.isNull()
            if not ready:
                errors.append("grabWindow 返回空图")
        if not ready:
            print("[步骤1] FAIL：QtCharts 在 offscreen+software 下不可用", flush=True)
            for e in errors:
                print("  QML 错误:", e, flush=True)
            raise SystemExit(2)
        view.destroy()
        elapsed = time.perf_counter() - t0
        print(f"[步骤1] QtCharts 最小 QML 加载+抓帧 OK（{elapsed * 1000:.0f} ms，"
              f"QApplication + offscreen + software）", flush=True)
        return elapsed
    finally:
        probe_qml.unlink(missing_ok=True)


def main() -> int:
    app = QApplication.instance() or QApplication([])

    # ---- 步骤 1：QtCharts 探针（最大风险点，先验证） ----
    probe_qtcharts(app)

    # ---- 步骤 2：CSV -> 13 点 -> set_series -> 断言 ----
    points = load_dense_points(DENSE_CSV)
    plateau = estimate_plateau(points)
    controller = SeriesController()
    controller.set_series(points, plateau, event_years=[2018])

    assert len(points) == 13, f"CSV 应解析出 13 期，实际 {len(points)}"
    assert len(controller.points) == 13, (
        f"controller.points 应为 13，实际 {len(controller.points)}"
    )
    assert 1.7 <= controller.plateau <= 1.9, f"plateau={controller.plateau} 越界"
    assert controller.plateau == plateau, "controller.plateau 应等于 estimate_plateau"
    assert list(controller.eventYears) == [2018], f"eventYears={controller.eventYears}"
    assert controller.has_plateau()

    # chartClicked（QML 调用的入口 Slot）-> pointClicked
    spy_click = QSignalSpy(controller.pointClicked)
    controller.chartClicked("2018-08-06")
    assert spy_click.count() == 1, f"pointClicked 应收到 1 次，实际 {spy_click.count()}"
    assert spy_click.at(0)[0] == "2018-08-06", f"收到 {spy_click.at(0)[0]!r}"
    print(f"[步骤2] 13 点入控制器；plateau={controller.plateau:.4f} km²（区间 [1.7,1.9] 内）；"
          f"chartClicked 直调 -> pointClicked 收到 '2018-08-06'", flush=True)

    # ---- QML 上屏：数据 -> 曲线 -> 高亮 -> 就近取点全链路 ----
    shell = _AppShell(controller)
    view = QQuickView()
    view.rootContext().setContextProperty("app", shell)
    view.setSource(QUrl.fromLocalFile(str(SERIES_QML)))
    errors = [e.toString() for e in view.errors()]
    assert view.status() == QQuickView.Status.Ready and not errors, (
        f"SeriesChart.qml 加载失败: {errors}"
    )
    view.resize(760, 480)
    view.show()
    for _ in range(8):
        app.processEvents()

    root = view.rootObject()
    assert root.property("shownPoints") == 13, (
        f"QML 曲线点数 {root.property('shownPoints')} != 13"
    )
    assert root.property("shownHighlights") == 0

    controller.highlight("2018-08-06")
    for _ in range(8):
        app.processEvents()
    assert controller.highlighted == "2018-08-06"
    assert root.property("shownHighlights") == 1, (
        f"QML 高亮点数 {root.property('shownHighlights')} != 1"
    )

    # 就近取点：把 2018-08-06 的像素坐标交给 pickNearest（MouseArea 同一函数），
    # 断言 pointClicked 收到同一天。JS Date.parse("2018-08-06") = UTC 午夜。
    t_target = int(datetime(2018, 8, 6, tzinfo=timezone.utc).timestamp() * 1000)
    px = root.pixelOfTime(t_target)
    pa = root.plotAreaRect()
    # Python 侧 QRectF 的 y/width/height 是方法（QML 值类型才是属性）
    py = pa.y() + pa.height() / 2.0
    spy_click2 = QSignalSpy(controller.pointClicked)
    root.pickNearest(px, py)
    assert spy_click2.count() == 1, f"pickNearest 应触发 pointClicked，实际 {spy_click2.count()}"
    assert spy_click2.at(0)[0] == "2018-08-06", f"就近取点得到 {spy_click2.at(0)[0]!r}"
    print(f"[步骤2-QML] 曲线 13 点上屏；高亮 1 点；plotArea({pa.width():.0f}x{pa.height():.0f}) "
          f"就近取点 -> pointClicked('2018-08-06')", flush=True)

    # ---- 步骤 3：ExtractWorker 实提取克亚吉尔小景 ----
    # 任务书参数（threshold=0.15, min=0.005）先照跑。实测该景 NDWI max=0.167、
    # p99=0.04（浑浊冰湖 + 9 月末高寒，湖体结冰/被碎屑覆盖，见 output/kyagar/
    # preview.png 的 NDWI 面板 water=0.3%），0.15 之下没有像元能过阈值，
    # extracted 合法地发回 0 个记录——worker 行为正确，不是失败。
    # "records >= 1" 的断言落在实测可检出的 threshold=0.05 上（恰好 1 个水体）。
    # 两次运行的真实数字都打进冒烟输出，偏差写进汇报。
    worker = ExtractWorker(str(SCENE_TIF), threshold=0.15, min_area_km2=0.005)
    spy_ok = QSignalSpy(worker.extracted)
    spy_fail = QSignalSpy(worker.failed)
    progress_texts: list[str] = []
    worker.progressed.connect(progress_texts.append)
    worker.start()
    ok = wait_for_signal(spy_ok, 180.0, app)
    worker.wait(5_000)
    assert ok, (
        f"extracted 未在 180s 内到达；failed={spy_dump(spy_fail)}；"
        f"progress={progress_texts}"
    )
    assert spy_fail.count() == 0, f"failed 被触发: {spy_dump(spy_fail)}"
    records_015 = spy_ok.at(0)[1]
    scene_obj = spy_ok.at(0)[0]
    assert isinstance(records_015, list), "extracted 第二参数应是 list"
    print(f"[步骤3a] ExtractWorker（任务书参数 threshold=0.15, min=0.005）："
          f"{len(records_015)} 个水体（实测 NDWI max=0.167，低于阈值，0 个是真实结果）；"
          f"progress {len(progress_texts)} 条；场景 {scene_obj.shape[1]}x{scene_obj.shape[0]}",
          flush=True)

    worker2 = ExtractWorker(str(SCENE_TIF), threshold=0.05, min_area_km2=0.005)
    spy_ok2 = QSignalSpy(worker2.extracted)
    spy_fail2 = QSignalSpy(worker2.failed)
    worker2.start()
    ok3 = wait_for_signal(spy_ok2, 180.0, app)
    worker2.wait(5_000)
    assert ok3, f"extracted(thr=0.05) 未到达；failed={spy_dump(spy_fail2)}"
    records = spy_ok2.at(0)[1]
    assert len(records) >= 1, f"records 应 >= 1，实际 {len(records)}"
    print(f"[步骤3b] ExtractWorker（threshold=0.05, min=0.005）：{len(records)} 个水体"
          f"（#1 {records[0].area_km2:.4f} km² / {records[0].n_pixels} px / "
          f"mean_ndwi={records[0].mean_ndwi:.3f}）", flush=True)

    # ---- 步骤 4：SeriesWorker years=[] -> 空序列、几秒内结束、取消不误报 ----
    sw = SeriesWorker(AOI_BBOX, years=[])
    spy_pts = QSignalSpy(sw.finished_pts)
    spy_fail2 = QSignalSpy(sw.failed)
    # 诚实语义验证：years=[] 时 progress 回调永远不触发，cancel() 不可能生效——
    # 预期照常发 finished_pts([])，绝不报"已取消"
    sw.cancel()
    t0 = time.perf_counter()
    sw.start()
    t0 = time.perf_counter()
    ok4 = wait_for_signal(spy_pts, 10.0, app)
    elapsed = time.perf_counter() - t0
    sw.wait(3_000)
    assert ok4, f"finished_pts 未在 10s 内到达；failed={spy_dump(spy_fail2)}"
    pts4 = spy_pts.at(0)[0]
    assert isinstance(pts4, list) and len(pts4) == 0, f"应发空 list，实际 {pts4!r}"
    assert spy_fail2.count() == 0, "空年份不应报 failed（含'已取消'）"
    assert elapsed < 5.0, f"空年份跑了 {elapsed:.1f}s，超出'几秒内'"
    print(f"[步骤4] SeriesWorker years=[] -> finished_pts([])，耗时 {elapsed * 1000:.0f} ms；"
          f"cancel() 未误报（诚实语义成立）", flush=True)

    # ---- 附加覆盖：clear() -> QML 回到空态，再整组恢复 ----
    # 契约 API clear() 与"points 为空显示提示"的路径不能没人踩过。
    controller.clear()
    for _ in range(8):
        app.processEvents()
    assert len(controller.points) == 0 and not controller.has_plateau()
    assert root.property("shownPoints") == 0
    assert root.property("hasData") is False
    controller.set_series(points, plateau, event_years=[2018])
    controller.highlight("2018-08-06")
    for _ in range(8):
        app.processEvents()
    assert root.property("shownPoints") == 13, "恢复后应回到 13 点"
    assert root.property("shownHighlights") == 1, "恢复后高亮应回到 1"
    print(f"[附加] clear() -> QML 空态(shownPoints=0, hasData=False) -> "
          f"set_series 恢复 13 点 + 高亮 1 点", flush=True)

    # ---- 步骤 5：截图存证 ----
    for _ in range(8):
        app.processEvents()
    img = view.grabWindow()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png = OUT_DIR / "series.png"
    assert not img.isNull() and img.save(str(png)), f"截图保存失败: {png}"
    print(f"[步骤5] 截图 {png.relative_to(ROOT)}（{img.width()}x{img.height()}）", flush=True)

    # 收尾：先把 QML 树同步拆掉再销毁窗口。直接让解释器关停时销毁，
    # 会在上下文属性失效后重估绑定，打两行无意义的 TypeError 噪音。
    # PySide6 的 QQuickItem 没有 QML 侧的 destroy()；deleteLater 又在
    # processEvents 这个循环层级不被处理，所以用 shiboken6.delete() 同步删。
    view.close()
    root_item = view.rootObject()
    if root_item is not None:
        shiboken6.delete(root_item)
    app.processEvents()
    view.destroy()
    print("SERIES SMOKE PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
