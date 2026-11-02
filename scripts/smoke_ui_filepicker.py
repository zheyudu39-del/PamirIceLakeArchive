"""冒烟：影像路径的「浏览…」文件选择（offscreen，不联网、不弹真窗口）。

运行（项目根目录）::

    .venv\\Scripts\\python.exe scripts/smoke_ui_filepicker.py

验证的是**路径转换**，不是系统对话框本身——对话框弹出来点哪个文件是
人工操作，自动化测试没法代劳，也不该代劳。能自动验的是最容易出错、
且错了最难发现的中间环节：

  QUrl（可能带中文/空格/百分号转义）
    -> AppState.pathFromUrl
    -> 面板里的文本
    -> 提取时 AppState._resolve_input_path 能不能真的找到这个文件

QML 侧自己删 "file://" 的写法在这条链上会断在第三环：`%E5%8D%A1` 这类
转义不会被还原，rasterio 拿到一个不存在的文件名。所以本脚本专门用带
中文和空格的目录名做用例。

三个断言组：
  1. FileDialog 与「浏览…」按钮存在，对话框起始目录是契约素材所在目录；
  2. pathFromUrl 对中文/空格/相对/非本地 URL 的转换结果；
  3. 选中的路径真的能通过 _resolve_input_path（即提取能读到它）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- Qt 环境变量必须在 import PySide6 之前设置 ----
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from glof.ui.app_state import DEFAULT_INPUT, PROJECT_ROOT, AppState  # noqa: E402
from glof.ui.image_provider import RasterImageProvider  # noqa: E402

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


def pump(app: QApplication, seconds: float = 0.1) -> None:
    """processEvents 轮询（QSignalSpy.wait 会阻塞 GIL，见 CLAUDE.md）。"""
    import time
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def find(win: QObject, name: str):
    return win.findChild(QObject, name)


def main() -> int:
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
        print("FILEPICKER SMOKE FAIL: Main.qml 加载失败", file=sys.stderr)
        for w in warnings:
            print("QML:", w, file=sys.stderr)
        return 1
    win = engine.rootObjects()[0]
    pump(app, 0.3)

    # ---- 1) 结构 ----
    print("== 步骤 1 / 控件与起始目录 ==", flush=True)
    browse = find(win, "browseButton")
    check("「浏览…」按钮存在", browse is not None)
    if browse is not None:
        check("按钮文字为「浏览…」", str(browse.property("text")) == "浏览…",
              str(browse.property("text")))
        check("未在提取/检索中时按钮可用", bool(browse.property("enabled")))

    dialog = find(win, "fileDialog")
    check("FileDialog 存在", dialog is not None)
    if dialog is not None:
        check("对话框标题含「选择」", "选择" in str(dialog.property("title")),
              str(dialog.property("title")))
        filters = dialog.property("nameFilters")
        joined = " ".join(str(f) for f in filters) if filters else ""
        check("过滤器含 .tif 与 .tiff", ".tif" in joined and ".tiff" in joined, joined)

    # 起始目录 = 契约素材所在目录（不是用户主目录）
    start = state.defaultInputDir()
    check("起始目录是 QUrl", isinstance(start, QUrl), str(type(start)))
    start_path = Path(start.toLocalFile()).resolve() if isinstance(start, QUrl) else None
    expected = (PROJECT_ROOT / Path(DEFAULT_INPUT).parent).resolve()
    check("起始目录 = 契约素材所在目录", start_path == expected,
          f"{start_path} 期望 {expected}")
    check("起始目录真实存在", start_path is not None and start_path.is_dir(),
          str(start_path))

    # ---- 2) QUrl -> 本地路径转换 ----
    print("== 步骤 2 / QUrl 转本地路径 ==", flush=True)
    # 用临时目录造真实文件名，覆盖三种会让 naive 写法出错的情况：
    #   中文+空格（真实素材常见）、# 和 %（toString 会保持转义）、
    #   以及盘符前导斜杠（所有 Windows 路径都会中招）。
    with tempfile.TemporaryDirectory() as tmp:
        odd_dir = Path(tmp) / "冰湖 影像"
        odd_dir.mkdir()
        odd_file = odd_dir / "克亚吉尔 2025-09-30.tif"
        odd_file.write_bytes(b"II*\x00")  # 只需存在，不真读

        url = QUrl.fromLocalFile(str(odd_file))
        # 记录 naive 写法会错在哪，断言才有意义（不是凭空说"更安全"）
        naive = url.toString().replace("file://", "")
        check("naive 写法会留下盘符前导斜杠（证明这个槽有必要）",
              naive.startswith("/") and not odd_file.as_posix().startswith("/"),
              naive)

        converted = state.pathFromUrl(url)
        check("转换结果不带盘符前导斜杠", not converted.startswith("/"), converted)
        check("转换结果等于原始路径（正斜杠）",
              converted == odd_file.as_posix(), converted)
        check("转换结果指向真实存在的文件", Path(converted).is_file(), converted)

        # 关键：提取链路能不能解析它（CWD 不在那个目录）
        resolved = AppState._resolve_input_path(converted)
        check("提取链路 _resolve_input_path 能解析该路径",
              resolved is not None and resolved.resolve() == odd_file.resolve(),
              str(resolved))

        # # 和 % 是 toString 保留转义的字符，toLocalFile 会还原
        for tricky in ("a#b.tif", "a%b.tif"):
            f = odd_dir / tricky
            f.write_bytes(b"II*\x00")
            got = state.pathFromUrl(QUrl.fromLocalFile(str(f)))
            check(f"文件名含 {tricky[1]} 时能正确还原", got == f.as_posix(), got)

    # 相对路径原样返回（不擅自拼项目根，交给 _resolve_input_path 的两段查找）
    rel = state.pathFromUrl(QUrl("data/kyagar/some.tif"))
    check("相对路径原样返回", rel == "data/kyagar/some.tif", rel)

    # 非本地 URL 不被伪装成本地路径
    remote = state.pathFromUrl(QUrl("https://example.com/a.tif"))
    check("非本地 URL 不伪造成本地路径", not remote.startswith("/")
          and "example.com" in remote, remote)

    # ---- 3) 面板文本联动 ----
    print("== 步骤 3 / 选中后写回输入框 ==", flush=True)
    field = find(win, "inputField")
    check("影像路径输入框存在", field is not None)
    if field is not None and dialog is not None:
        check("输入框初始值 = 契约素材", str(field.property("text")) == DEFAULT_INPUT,
              str(field.property("text")))
        # 模拟 onAccepted 里那一行：selectedFile -> pathFromUrl -> 输入框
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "新 影像.tif"
            target.write_bytes(b"II*\x00")
            dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(target)))
            pump(app, 0.1)
            field.setProperty("text", state.pathFromUrl(dialog.property("selectedFile")))
            pump(app, 0.1)
            text = str(field.property("text"))
            check("输入框被改成选中的文件", text == target.as_posix(), text)
            check("输入框路径可被提取链路解析",
                  AppState._resolve_input_path(text) is not None, text)

    check("QML 全程零警告", not warnings, f"{warnings[:3]}" if warnings else "")

    n_pass = sum(1 for _, ok, _ in _checks if ok)
    print(f"\n共 {len(_checks)} 项断言，通过 {n_pass} 项", flush=True)
    if n_pass == len(_checks):
        print("FILEPICKER SMOKE PASS", flush=True)
        return 0
    for name, ok, detail in _checks:
        if not ok:
            print(f"  FAIL: {name}   {detail}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
