"""冒烟：湖泊/风险列表模块（LakeModel + LakeTable.qml）。

目的（对应 docs §2.3 的 lake_table 组件）：本地素材 → core 提取 → 模型 →
QML 表格 → 点击联动 → 判据详情着色 → 截图，一条链路真跑通。

运行（cmd，无显示器）::

    .venv\\Scripts\\python.exe scripts\\smoke_ui_table.py

素材说明（诚实性，先说清楚）：
    契约指定的 data/kyagar/...T43SGV...tif（285x906 小景）在契约参数
    (threshold=0.15, min_area_km2=0.005) 下**实测提出 0 条**：该景
    2025-09-30 湖面冰封/高浊度，全景 NDWI 最大仅 0.167、NDWI>0.15 只有
    1 个像元（冒烟输出会打印这些数字）。因此回退到本地已有的
    data/spike/...T43SFV...tif（克亚吉尔同一湖区 2025-09-28 的全湖窗口），
    同参数实测提出 200+ 条。全程只用本地素材，无联网。

    risk_level / risk_reasons 是手工赋的**演示数据**：reasons 一律用
    glof.core.risk 的 assess_ice_dammed / assess_moraine_dammed 生成，
    文本格式与 Criterion.describe() 完全一致，详情面板的前缀着色才能按
    真实格式验证。
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

# 必须在 import PySide6 之前：无显示器环境 + software 渲染
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
# offscreen 平台不带字体（Qt 已不再随包发布字体），不指路则中文/数字全是豆腐块
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, QPoint, QUrl, Property, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtQuick import QQuickView  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402

from glof.core.indices import BAND_PRESETS, ndwi  # noqa: E402
from glof.core.io import load_scene  # noqa: E402
from glof.core.risk import assess_ice_dammed, assess_moraine_dammed  # noqa: E402
from glof.core.water import LakeRecord, extract_lakes  # noqa: E402
from glof.ui.lake_model import LakeModel  # noqa: E402
from glof.ui.style import CRITERION_STATUS_COLORS, risk_color  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

THRESHOLD = 0.15
MIN_AREA_KM2 = 0.005

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    return bool(ok)


class Shell(QObject):
    """主窗口的占位：QML 经上下文属性 app.lakes 访问 LakeModel（契约约定）。"""

    def __init__(self, lakes: LakeModel) -> None:
        super().__init__()
        self._lakes = lakes

    @Property(QObject, constant=True)
    def lakes(self) -> LakeModel:
        return self._lakes


def extract_records() -> tuple[list[LakeRecord], str]:
    """契约路径：data/kyagar 小景 + (0.15, 0.005)；提出 0 条时回退 data/spike 景。

    返回 (记录, 实际使用的素材说明)。两个素材都失败则返回空表。
    """
    plan = [
        ("kyagar", "契约指定素材（T43SGV 2025-09-30 小景）"),
        ("spike", "回退素材（T43SFV 2025-09-28 同湖全湖窗口）"),
    ]
    for sub, label in plan:
        tif = next((ROOT / "data" / sub).glob("*.tif"))
        t0 = time.perf_counter()
        scene = load_scene(str(tif), BAND_PRESETS["s2_l2a_6band"])
        nd = ndwi(scene["green"], scene["nir"])
        records = extract_lakes(
            nd, scene.transform, scene.crs,
            threshold=THRESHOLD, min_area_km2=MIN_AREA_KM2,
        )
        print(
            f"  素材[{label}] {tif.name} {scene.shape[1]}x{scene.shape[0]}，"
            f"读+提取 {(time.perf_counter() - t0) * 1000:.0f} ms，"
            f"提取 {len(records)} 条（threshold={THRESHOLD}, min_area_km2={MIN_AREA_KM2}）"
        )
        if records:
            return records, label
    return [], "两个本地素材在契约参数下都提出 0 条"


def assign_demo_risk(records: list[LakeRecord]) -> dict[int, str]:
    """手工给 4 条赋等级（演示数据，非流水线判定），覆盖 HIGH/MEDIUM/LOW/UNKNOWN。

    reasons 全部经 glof.core.risk 的 assess_* 生成，保证与 Criterion.describe()
    同一格式；等级构成与具体面积无关（平台/基准都由该记录面积推出），可复现。
    """
    marks: dict[int, str] = {}

    r0 = records[0]
    # 冰坝湖：平台面积取该湖面积的 0.98 倍 -> 比值约 1.02 > 0.95，判据命中
    a0 = assess_ice_dammed(
        r0.area_km2, plateau_area_km2=round(r0.area_km2 * 0.98, 4), label="演示"
    )
    r0.risk_level = a0.level.value
    r0.risk_reasons = a0.reasons
    marks[r0.lake_id] = r0.risk_level

    if len(records) >= 3:
        r1 = records[1]
        # 冰碛坝：年增速 11.1% > 2% 命中，坡度/母冰川接触缺数据 -> MEDIUM
        a1 = assess_moraine_dammed(
            r1.area_km2, baseline_area_km2=r1.area_km2 * 0.9,
            baseline_year=2024.0, year=2025.0, label="演示",
        )
        r1.risk_level = a1.level.value
        r1.risk_reasons = a1.reasons
        marks[r1.lake_id] = r1.risk_level

        r2 = records[2]
        # 冰碛坝：增速为负、坡度 5<10、不接触母冰川，三条全未命中且无缺口 -> LOW
        a2 = assess_moraine_dammed(
            r2.area_km2, baseline_area_km2=r2.area_km2 * 1.2,
            baseline_year=2024.0, year=2025.0,
            downstream_slope_deg=5.0, glacier_contact=False, label="演示",
        )
        r2.risk_level = a2.level.value
        r2.risk_reasons = a2.reasons
        marks[r2.lake_id] = r2.risk_level

    rlast = records[-1]
    # 冰碛坝：三项数据全缺 -> UNKNOWN（缺数据不判低）
    a3 = assess_moraine_dammed(rlast.area_km2, label="演示")
    rlast.risk_level = a3.level.value
    rlast.risk_reasons = a3.reasons
    marks[rlast.lake_id] = rlast.risk_level
    return marks


def step_hello_qml() -> None:
    """步骤 1：最小 hello QML，验证 QQml 工具链本身可用。"""
    print("步骤 1 / hello QML 工具链")
    engine = QQmlEngine()
    comp = QQmlComponent(engine)
    comp.setData(
        b"import QtQuick\nItem { objectName: 'hello'; property bool ok: true }",
        QUrl("hello.qml"),
    )
    obj = comp.create()
    ok = comp.isReady() and obj is not None and bool(obj.property("ok"))
    check("hello QML 加载与实例化", ok,
          f"status={comp.status()}, errors={comp.errors()}")
    print()


def step_model(records: list[LakeRecord]) -> LakeModel:
    """步骤 2：set_records + 全行角色断言 + clear 往返。"""
    print("步骤 3 / LakeModel（set_records + 角色）")
    model = LakeModel()
    model.set_records(records)

    check("rowCount == 提取条数", model.rowCount() == len(records),
          f"{model.rowCount()} == {len(records)}")
    check("columnCount == 7", model.columnCount() == 7, f"{model.columnCount()}")
    headers = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(7)]
    check("中文表头", headers == ["ID", "面积 km²", "周长 km", "平均NDWI", "经度", "纬度", "风险等级"],
          f"{headers}")
    names = {bytes(n) for n in model.roleNames().values()}
    need = {b"lakeId", b"areaKm2", b"perimeterKm", b"meanNdwi", b"lon", b"lat",
            b"level", b"color", b"reasons", b"nPixels", b"display"}
    check("roleNames 全齐", need <= names, f"缺 {sorted(need - names)}")

    n_bad = 0
    first_bad = ""
    for row in range(model.rowCount()):
        idx = model.index(row, 0)
        area = float(model.data(idx, LakeModel.ROLE_AREA_KM2))
        perim = float(model.data(idx, LakeModel.ROLE_PERIMETER_KM))
        level = str(model.data(idx, LakeModel.ROLE_LEVEL))
        color = str(model.data(idx, LakeModel.ROLE_COLOR))
        lon = float(model.data(idx, LakeModel.ROLE_LON))
        lat = float(model.data(idx, LakeModel.ROLE_LAT))
        npix = int(model.data(idx, LakeModel.ROLE_N_PIXELS))
        if not (area > 0 and perim > 0 and level and color.startswith("#")
                and color == risk_color(level) and math.isfinite(lon)
                and math.isfinite(lat) and npix > 0):
            n_bad += 1
            first_bad = first_bad or f"row {row}: area={area} level={level!r} color={color!r}"
    check(f"全 {model.rowCount()} 行角色合法（areaKm2>0、level 非空、color=#且与 risk_color 一致）",
          n_bad == 0, f"非法 {n_bad} 行 {first_bad}")

    # DisplayRole 格式化抽查
    idx0 = model.index(0, 1)
    check("面积列 4 位小数", model.data(idx0) == f"{records[0].area_km2:.4f}",
          f"{model.data(idx0)}")
    check("周长列 2 位小数", model.data(model.index(0, 2)) == f"{records[0].perimeter_m / 1000:.2f}",
          f"{model.data(model.index(0, 2))}")

    check("record_at(0) 即第一条", model.record_at(0) is records[0])
    mid = min(5, len(records) - 1)
    check("row_of 往返一致", model.row_of(records[mid].lake_id) == mid,
          f"{model.row_of(records[mid].lake_id)} == {mid}")
    check("row_of 不存在 -> -1", model.row_of(-999) == -1)
    check("record_at 越界 -> None", model.record_at(10 ** 6) is None)

    model.clear()
    check("clear 后 rowCount==0", model.rowCount() == 0)
    model.set_records(records)
    check("重新 set_records 后 rowCount 恢复", model.rowCount() == len(records))
    print()
    return model


def step_window(model: LakeModel) -> tuple[QQuickView, Shell]:
    """步骤 4：挂上下文属性 app.lakes，加载 LakeTable.qml。

    Shell 一并返回由 main 持引用——setContextProperty 不增加引用计数，
    Python 侧引用一丢，C++ 对象销毁，QML 里的 app 就成 null。
    """
    print("步骤 4 / LakeTable.qml 加载")
    view = QQuickView()
    shell = Shell(model)
    view.engine().rootContext().setContextProperty("app", shell)
    view.setSource(QUrl.fromLocalFile(str(ROOT / "glof" / "ui" / "qml" / "LakeTable.qml")))
    ready = view.status() == QQuickView.Status.Ready
    check("LakeTable.qml 加载 Ready", ready,
          "" if ready else f"errors={[str(e) for e in view.errors()]}")
    check("无 QML 运行错误", not view.errors(),
          "" if not view.errors() else f"{[str(e) for e in view.errors()]}")
    view.resize(860, 640)
    view.show()
    check("窗口可见（offscreen）", QTest.qWaitForWindowExposed(view, 5000))
    print()
    return view, shell


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])

    step_hello_qml()

    print("步骤 2 / 本地素材提取")
    records, material = extract_records()
    if not check("契约参数下拿到记录", len(records) > 0, material):
        print("TABLE SMOKE FAIL")
        return 1
    marks = assign_demo_risk(records)
    levels = sorted(set(marks.values()))
    check("演示等级覆盖 HIGH 与 UNKNOWN",
          "HIGH" in levels and "UNKNOWN" in levels, f"覆盖 {levels}")
    print(f"  说明：risk_level/risk_reasons 为手工赋的演示值，reasons 由 risk.assess_* 生成")
    print()

    model = step_model(records)
    view, _shell = step_window(model)  # _shell 持引用防 GC
    root = view.rootObject()

    # 无选中状态断言（契约：无选中显示"点击行查看判据明细"），须在点击/selectRow 之前。
    # 注意 RichText 的 text 属性读回是规范化 HTML，比对用 getText 的纯文本。
    detail0 = root.findChild(QObject, "detailArea")
    if detail0 is not None:
        plain0 = detail0.getText(0, int(detail0.property("length")))
        check("无选中时详情显示占位文案", plain0 == "点击行查看判据明细",
              f"text={plain0[:40]!r}")
    check("highlightedId 默认 -1", root.property("highlightedId") == -1,
          f"highlightedId={root.property('highlightedId')}")
    spy: QSignalSpy | None = None
    try:
        spy = QSignalSpy(root.lakeClicked)
    except Exception:
        from PySide6.QtCore import SIGNAL  # noqa: PLC0415  兼容回退
        spy = QSignalSpy(root, SIGNAL("lakeClicked(int)"))
    check("QSignalSpy 挂上 lakeClicked", spy is not None and spy.isValid(),
          f"count={spy.count()}")

    print("步骤 5 / 点击第一行 -> lakeClicked")
    p = root.delegateCenter(0, 0)
    check("第 0 行 delegate 已实例化", float(p.x()) > 0,
          f"中心=({float(p.x()):.0f},{float(p.y()):.0f})（mapToItem(null) 场景坐标）")
    QTest.mouseClick(
        view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        QPoint(int(float(p.x())), int(float(p.y()))),
    )
    QGuiApplication.processEvents()
    check("lakeClicked 触发 1 次", spy.count() == 1, f"count={spy.count()}")
    got = int(spy.at(0)[0]) if spy.count() else -1
    check("lakeClicked 参数 == 第一行 lakeId", got == records[0].lake_id,
          f"{got} == {records[0].lake_id}")
    check("点击后 highlightedId 跟随", root.property("highlightedId") == records[0].lake_id,
          f"highlightedId={root.property('highlightedId')}")
    print()

    print("步骤 6 / selectRow 程序化选中（不发信号）")
    target = records[-1]
    before = spy.count()
    root.selectRow(target.lake_id)
    QGuiApplication.processEvents()
    check("selectRow 后 highlightedId 变为该湖",
          root.property("highlightedId") == target.lake_id,
          f"highlightedId={root.property('highlightedId')} == {target.lake_id}")
    check("lakeClicked 计数不变（防回环）", spy.count() == before,
          f"{before} -> {spy.count()}")
    print()

    print("步骤 7 / 判据详情着色")
    out_dir = ROOT / "output" / "ui_smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    detail = root.findChild(QObject, "detailArea")
    check("找到详情 TextArea", detail is not None)
    if detail is not None:
        text = detail.getText(0, int(detail.property("length")))
        expected = target.risk_reasons[0][:10]
        check("详情含所选记录第一条 reason 前 10 字符", expected in text,
              f"“{expected}” in 详情文本")
        # 三色逐条着色走模型带出的 CRITERION_STATUS_COLORS：
        # MEDIUM（命中+缺数据）、LOW（全未命中）、UNKNOWN（全缺）各选一次，
        # 富文本源码里应出现对应色值。
        root.selectRow(records[1].lake_id)
        QGuiApplication.processEvents()
        html_med = str(detail.property("text"))
        check("命中行用 style 红色", CRITERION_STATUS_COLORS["HIT"] in html_med)
        check("缺数据行用 style 灰色", CRITERION_STATUS_COLORS["UNKNOWN"] in html_med)
        # 附加视觉证据：MEDIUM 记录 = 红（命中）+ 灰（缺数据）混合
        med_path = out_dir / "table_detail.png"
        med_ok = view.grabWindow().save(str(med_path))
        check("附加截图 table_detail.png", med_ok and med_path.exists()
              and med_path.stat().st_size > 0, str(med_path))
        root.selectRow(records[2].lake_id)
        QGuiApplication.processEvents()
        html_low = str(detail.property("text"))
        check("未命中行用 style 绿色", CRITERION_STATUS_COLORS["MISS"] in html_low)
        # 回到最后一条 UNKNOWN 记录，作为截图状态
        root.selectRow(target.lake_id)
        QGuiApplication.processEvents()
    print()

    print("步骤 8 / 截图存证")
    shot_path = out_dir / "table.png"
    shot = view.grabWindow()
    saved = shot.save(str(shot_path))
    check("grabWindow 保存 table.png",
          saved and shot_path.exists() and shot_path.stat().st_size > 0,
          f"{shot_path}（{shot_path.stat().st_size if shot_path.exists() else 0} 字节）")
    print()

    n_fail = sum(1 for _, ok, _ in CHECKS if not ok)
    print(f"共 {len(CHECKS)} 项断言，通过 {len(CHECKS) - n_fail}，失败 {n_fail}")
    if n_fail == 0:
        print("TABLE SMOKE PASS")
        return 0
    print("TABLE SMOKE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
