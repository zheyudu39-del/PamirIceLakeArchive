"""冒烟：「刷新最新观测」按钮 + LatestWorker + QML 三态显示（offscreen，不联网）。

运行（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\smoke_ui_latest.py

为什么用 QApplication：Main.qml 底部是 QtCharts 的 ChartView，它在 C++ 层
链接 QtWidgets——QGuiApplication 下一构造就访问违例（见
scripts/smoke_ui_series.py 的环境硬规则 1）。offscreen / software / 字体
目录三条也在 import PySide6 之前设好。

为什么不会联网：本脚本把 ``glof.core.latest.fetch_latest`` 换成桩函数。
LatestWorker 是经模块属性（``core_latest.fetch_latest``）调用它的，所以
monkeypatch 真的生效——这也顺带验证了 worker 没有在 import 时把函数对象
绑死（绑死的话冒烟会真的去联网）。

桩用一个 threading.Event 卡住返回，让主线程有机会观察到 ``latestBusy``
为 True；随后放行，走完 worker 信号 → AppState 槽 → QML 属性这一整条链。
注意不能用 ``QSignalSpy.wait()``：它阻塞期间不放 GIL，工作线程拿不到解释器
（CLAUDE.md 记录的已知坑），统一用 processEvents 轮询。

三态断言（与 Main.qml 的措辞一一对应）：
  a) fetch_latest 返回 None        -> 暂无可用观测
  b) 有观测但 total_area_km2=None  -> 面积没量到（无水面信号）+ 季节警告
  c) 正常观测                      -> 观测日期 / 距今 N 天 / 面积 / 湖泊数
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- Qt 环境变量必须在 import PySide6 之前设置 ----
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from glof.core import latest as core_latest  # noqa: E402
from glof.core.latest import LatestObservation  # noqa: E402
from glof.core.water import LakeRecord  # noqa: E402
from glof.ui.app_state import AppState  # noqa: E402
from glof.ui.image_provider import RasterImageProvider  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

QML_DIR = ROOT / "glof" / "ui" / "qml"
OUT_DIR = ROOT / "output" / "ui_smoke"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""),
          flush=True)
    return bool(ok)


def pump(app: QApplication, seconds: float) -> None:
    """processEvents 轮询等待。sleep 每轮让出 GIL 给工作线程。"""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)


def wait_until(app: QApplication, predicate, timeout_s: float) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def make_record(lake_id: int, area_km2: float, lon: float, lat: float) -> LakeRecord:
    return LakeRecord(
        lake_id=lake_id,
        area_m2=area_km2 * 1e6,
        perimeter_m=1000.0,
        n_pixels=100,
        area_px_m2=area_km2 * 1e6,
        mean_ndwi=0.4,
        centroid_lon=lon,
        centroid_lat=lat,
        geometry_wgs84_json="{}",
    )


class FakeFetch:
    """fetch_latest 的离线桩：记录调用参数，按 gate 控制返回时机。"""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.calls: list[dict] = []
        self.result: LatestObservation | None = None

    def __call__(self, bbox, *, lookback_days=20, max_cloud=20.0,
                 min_area_km2=0.01, today=None, progress=None):
        self.calls.append({
            "bbox": tuple(bbox), "lookback_days": lookback_days,
            "max_cloud": max_cloud, "min_area_km2": min_area_km2,
        })
        if progress is not None:
            progress("冒烟桩：检索影像")
        # 卡住返回，让主线程观察到 latestBusy=True
        self.gate.wait(15.0)
        return self.result


def find(win: QObject, name: str):
    return win.findChild(QObject, name)


def label_text(win: QObject, name: str) -> str:
    item = find(win, name)
    return str(item.property("text")) if item is not None else ""


def label_visible(win: QObject, name: str) -> bool:
    item = find(win, name)
    return bool(item.property("visible")) if item is not None else False


def main() -> int:
    app = QApplication.instance() or QApplication([])

    fake = FakeFetch()
    original = core_latest.fetch_latest
    core_latest.fetch_latest = fake  # LatestWorker 经模块属性调用，patch 生效
    try:
        engine = QQmlApplicationEngine()
        provider = RasterImageProvider()
        engine.addImageProvider(RasterImageProvider.PROVIDER_ID, provider)
        state = AppState(provider)
        engine.rootContext().setContextProperty("app", state)
        engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
        if not engine.rootObjects():
            print("LATEST SMOKE FAIL: Main.qml 加载失败", file=sys.stderr)
            return 1
        win = engine.rootObjects()[0]
        pump(app, 0.3)

        # ---- 结构：按钮存在、文字正确、初始态 ----
        print("== 步骤 1 / 按钮与初始态 ==", flush=True)
        button = find(win, "latestButton")
        check("找到「刷新最新观测」按钮", button is not None)
        check("按钮文字为「刷新最新观测」（不写“实时”）",
              button is not None and str(button.property("text")) == "刷新最新观测",
              str(button.property("text")) if button is not None else "None")
        check("初始 latestBusy 为 False", state.property("latestBusy") is False)
        check("初始 latestAgeDays 为 -1", int(state.property("latestAgeDays")) == -1,
              f"{state.property('latestAgeDays')}")
        check("初始 latestAreaKm2 为 NaN（不是 0）",
              math.isnan(float(state.property("latestAreaKm2"))),
              f"{state.property('latestAreaKm2')}")
        check("初始结果区隐藏（没观测）", not label_visible(win, "latestPanel"))

        # ---- 状态 a：fetch_latest 返回 None -> 暂无可用观测 ----
        print("== 步骤 2 / 点击按钮（桩返回 None）==", flush=True)
        fake.result = None
        fake.gate.clear()
        button.clicked.emit()
        check("点击后 latestBusy 立即变 True",
              wait_until(app, lambda: bool(state.property("latestBusy")), 2.0),
              f"latestBusy={state.property('latestBusy')}")
        check("加载中提示可见", label_visible(win, "latestLoadingLabel"))
        check("加载中结果区隐藏", not label_visible(win, "latestPanel"))

        fake.gate.set()
        ok = wait_until(app, lambda: not bool(state.property("latestBusy")), 10.0)
        check("完成后 latestBusy 复位", ok)
        check("latestAgeDays 仍为 -1（暂无观测，不是 0 天）",
              int(state.property("latestAgeDays")) == -1,
              f"{state.property('latestAgeDays')}")
        msg = str(state.property("latestMessage"))
        check("latestMessage 含「暂无可用观测」", "暂无可用观测" in msg, msg)
        empty_text = label_text(win, "latestEmptyLabel")
        check("QML 空态文案 =「暂无可用观测…」",
              label_visible(win, "latestEmptyLabel") and "暂无可用观测" in empty_text,
              empty_text)
        check("空态不显示结果区", not label_visible(win, "latestPanel"))
        check("fetch_latest 收到的参数：bbox 来自面板、lookback=20",
              len(fake.calls) == 1
              and fake.calls[0]["bbox"] == (77.0249, 35.5427, 77.3567, 35.8121)
              and fake.calls[0]["lookback_days"] == 20,
              f"{fake.calls[0] if fake.calls else None}")

        # ---- 状态 b：有观测但没量到面积（None）-> 面积没量到 + 季节警告 ----
        print("== 步骤 3 / 有观测但无水面信号（total_area_km2=None）==", flush=True)
        fake.result = LatestObservation(
            observed_on="2026-10-28",
            age_days=5,
            scene_id="S2A_SMOKE_T43SFV",
            tile="T43SFV",
            cloud=2.2,
            coverage=1.0,
            records=[],
            total_area_km2=None,       # 契约：没量到必须是 None
            largest_area_km2=None,
            ndwi_max=0.01,
            season_warning="观测日期 10 月不在消融期（6–9 月），帕米尔湖面可能已结冰，NDWI 失效",
        )
        fake.gate.clear()
        button.clicked.emit()
        wait_until(app, lambda: bool(state.property("latestBusy")), 2.0)
        fake.gate.set()
        ok = wait_until(app, lambda: not bool(state.property("latestBusy")), 10.0)
        check("状态 b 完成", ok)
        check("latestAgeDays = 5", int(state.property("latestAgeDays")) == 5,
              f"{state.property('latestAgeDays')}")
        check("latestDate = 2026-10-28", str(state.property("latestDate")) == "2026-10-28",
              str(state.property("latestDate")))
        check("latestAreaKm2 是 NaN（不是 0）",
              math.isnan(float(state.property("latestAreaKm2"))),
              f"{state.property('latestAreaKm2')}")
        area_text = label_text(win, "latestAreaLabel")
        check("QML 面积文案 =「面积没量到（无水面信号）」",
              area_text == "面积没量到（无水面信号）", area_text)
        check("QML 面积文案不含「0.00 km²」", "0.00" not in area_text, area_text)
        check("季节警告显示在提示行",
              "结冰" in label_text(win, "latestMessageLabel")
              and label_visible(win, "latestMessageLabel"),
              label_text(win, "latestMessageLabel"))
        check("新鲜度行显示日期与距今天数",
              "2026-10-28" in label_text(win, "latestFreshnessLabel")
              and "距今 5 天" in label_text(win, "latestFreshnessLabel"),
              label_text(win, "latestFreshnessLabel"))

        # ---- 状态 c：正常观测 -> 日期 / 距今天数 / 面积 / 湖泊数 ----
        print("== 步骤 4 / 正常观测（2 个湖，合计 1.2345 km²）==", flush=True)
        fake.result = LatestObservation(
            observed_on="2026-08-14",
            age_days=80,
            scene_id="S2B_SMOKE_T43SGV",
            tile="T43SGV",
            cloud=8.5,
            coverage=1.0,
            records=[
                make_record(0, 1.0, 77.12, 35.70),
                make_record(1, 0.2345, 77.13, 35.71),
            ],
            total_area_km2=1.2345,
            largest_area_km2=1.0,
            ndwi_max=0.62,
            season_warning="",
        )
        fake.gate.clear()
        button.clicked.emit()
        wait_until(app, lambda: bool(state.property("latestBusy")), 2.0)
        fake.gate.set()
        ok = wait_until(app, lambda: not bool(state.property("latestBusy")), 10.0)
        check("状态 c 完成", ok)
        check("latestDate = 2026-08-14", str(state.property("latestDate")) == "2026-08-14",
              str(state.property("latestDate")))
        check("latestAgeDays = 80", int(state.property("latestAgeDays")) == 80,
              f"{state.property('latestAgeDays')}")
        check("latestAreaKm2 = 1.2345",
              abs(float(state.property("latestAreaKm2")) - 1.2345) < 1e-9,
              f"{state.property('latestAreaKm2')}")
        check("latestNLakes = 2", int(state.property("latestNLakes")) == 2,
              f"{state.property('latestNLakes')}")
        check("latestMessage 为空（无警告无错误）",
              str(state.property("latestMessage")) == "",
              str(state.property("latestMessage")))
        check("QML 面积/湖泊文案",
              label_text(win, "latestAreaLabel") == "当前面积 1.2345 km²　湖泊 2 个",
              label_text(win, "latestAreaLabel"))
        check("QML 提示行隐藏（无警告）", not label_visible(win, "latestMessageLabel"))
        check("QML 空态行隐藏", not label_visible(win, "latestEmptyLabel"))
        check("结果区可见", label_visible(win, "latestPanel"))

        # ---- 失败路径：worker 抛异常 -> failed -> latestMessage 写明 ----
        print("== 步骤 5 / 异常路径（桩抛异常）==", flush=True)

        def boom(*_a, **_kw):
            raise RuntimeError("冒烟桩故意失败")

        core_latest.fetch_latest = boom
        fake.gate.set()
        button.clicked.emit()
        ok = wait_until(app, lambda: not bool(state.property("latestBusy"))
                        and "检索失败" in str(state.property("latestMessage")), 10.0)
        check("异常走 failed：busy 复位且 latestMessage 写明检索失败", ok,
              str(state.property("latestMessage")))
        check("lastError 也写入了（横幅可见）",
              "冒烟桩故意失败" in str(state.property("lastError")),
              str(state.property("lastError")))

        # ---- 截图存证 ----
        pump(app, 0.2)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / "latest.png"
        img = win.grabWindow() if hasattr(win, "grabWindow") else None
        if img is not None and not img.isNull():
            saved = img.save(str(png))
            check("截图 latest.png 已保存",
                  saved and png.exists() and png.stat().st_size > 0,
                  f"{png}（{png.stat().st_size if png.exists() else 0} 字节）")
        else:
            print("  [SKIP] 根窗口不可直接 grabWindow，跳过截图（不影响断言）", flush=True)

        n_fail = sum(1 for _, ok, _ in CHECKS if not ok)
        print(f"\n共 {len(CHECKS)} 项断言，通过 {len(CHECKS) - n_fail}，失败 {n_fail}",
              flush=True)
        if n_fail == 0:
            print("LATEST SMOKE PASS", flush=True)
            return 0
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  FAIL: {name}   {detail}", flush=True)
        print("LATEST SMOKE FAIL", flush=True)
        return 1
    finally:
        core_latest.fetch_latest = original


if __name__ == "__main__":
    raise SystemExit(main())
