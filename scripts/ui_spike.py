"""UI spike：验证 QGraphicsView 能否流畅拖动 3000×3000 级别的真实影像。

这是开工界面前的最后一个 gate（docs §8.2）。它只回答一个问题：
**最简单的方案——整景解码成一张 QImage 塞进 QGraphicsPixmapItem——
缩放平移会不会卡？** 卡了才需要上"按视图窗口 + overview 分级读取"那套复杂方案。

被测的完整链路（与将来主窗口一致，docs §2.3）：

    glof.core.io 读栈（B11 20m 自动重采样到 10m）
      → 真彩色合成 B04/B03/B02 + 2%–98% 百分位拉伸
      → numpy uint8 → QImage（bytesPerLine 手动补齐 4 字节对齐）
      → QGraphicsPixmapItem → QGraphicsView
      → AnchorUnderMouse 滚轮缩放 + scroll() 平移

测什么
------
* 载入：读栈 + 重采样 + 拉伸 + QImage 构造的总耗时（界面启动的"白屏时间"）。
* 缩放：从 fit 到 4× 超采样逐级放大再退回，每步一次 transform 变更 + 强制同步重绘，
  记录单帧毫秒数。重绘用 ``viewport().grab()`` 强制同步走完整个 paint 流程，
  这是"滚轮转一下要等多久"的直接度量。
* 平移：连续横向/纵向 scroll()，同样逐帧计时。平移是最频繁的操作，比缩放更考验。
* 内存：进程 RSS 前后差 + 各缓冲区字节数，确认 100–200 MB 量级的预估成立。

判定线
------
    单帧 < 33 ms   流畅（30 fps 以上），PASS
    33–100 ms      有感但可用，结论里注明
    > 100 ms       卡，FAIL —— 必须改窗口化读取

为什么阈值取 33：滚轮缩放是离散事件（一格一格），人眼对 100 ms 内的响应
都觉得"跟手"；连续平移要 30 fps 才不闪。所以缩放用 100 ms 红线、平移用 33 ms。

运行（无显示器也能跑，offscreen 平台）：

    ./.venv/Scripts/python.exe scripts/ui_spike.py --input data/spike/<scene>.tif
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsView,
)

import numpy as np  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core.indices import BAND_PRESETS  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 计时与内存辅助
# ---------------------------------------------------------------------------

def rss_mb() -> float:
    """当前进程物理内存（MB）。只用于 spike 报数，不进主程序。

    restype/argtypes 必须显式声明：不声明时 ctypes 默认按 c_int 处理
    返回值与参数，64 位下结构体指针/句柄会被截断，调用静默失败返回 0。
    """
    import ctypes.wintypes as wt

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.K32GetProcessMemoryInfo.restype = wt.BOOL
    k32.K32GetProcessMemoryInfo.argtypes = [
        wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD,
    ]
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    handle = k32.GetCurrentProcess()
    if k32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return pmc.WorkingSetSize / 1024**2
    return float("nan")


def mb(n_bytes: int | float) -> str:
    return f"{n_bytes / 1024**2:.1f} MB"


def stretch_rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    """真彩色 2%–98% 百分位拉伸，输出 uint8 (h, w, 3)。

    百分位分波段独立取。边界裁剪后线性映射到 0–255。
    NaN（读栈后的无效区）进不了 uint8，先归 0——显示上就是黑底，
    与将来主窗口"无效区涂黑"的处理一致。
    """
    out = np.empty((r.shape[0], r.shape[1], 3), dtype=np.uint8)
    for i, band in enumerate((r, g, b)):
        finite = band[np.isfinite(band)]
        lo, hi = np.percentile(finite, (2, 98))
        scaled = np.nan_to_num(
            np.clip((band - lo) / max(hi - lo, 1e-9), 0.0, 1.0), nan=0.0
        )
        out[:, :, i] = (scaled * 255).astype(np.uint8)
    return out


def to_qimage(rgb8: np.ndarray) -> QImage:
    """numpy uint8 (h, w, 3) → QImage.Format_RGB888。

    QImage 要求每行字节数 4 字节对齐，而 w*3 不一定对齐（比如 2686*3=8058）。
    不处理这条，图会歪斜或崩——docs §2.3 说的"注意 stride"就是它。
    做法：多分配一条对齐的缓冲，逐行只填前 w*3 字节，bytesPerLine 显式传对齐值。
    numpy 数组必须由调用方保活（QImage 不拷贝外部内存），所以挂在返回值上。
    """
    h, w, _ = rgb8.shape
    stride = ((w * 3 + 3) // 4) * 4
    buf = np.zeros((h, stride), dtype=np.uint8)
    buf[:, : w * 3] = rgb8.reshape(h, w * 3)
    img = QImage(buf.data, w, h, stride, QImage.Format.Format_RGB888)
    img._buf = buf  # type: ignore[attr-defined]  保活，防 GC 后悬空指针
    return img


class Frame:
    """一次"transform 变更 + 同步重绘"的耗时样本。"""

    __slots__ = ("label", "ms")

    def __init__(self, label: str, ms: float):
        self.label = label
        self.ms = ms


def paint_once(view: QGraphicsView, label: str) -> Frame:
    """强制同步走一遍完整 paint，返回单帧耗时。

    ``grab()`` 会把 viewport 完整渲染进一个 QPixmap，与滚轮/拖动后
    系统触发的 paint 是同一条路径，因此耗时可以直接当作"单帧成本"。
    """
    t0 = time.perf_counter()
    pix = view.viewport().grab()
    QApplication.processEvents()
    ms = (time.perf_counter() - t0) * 1000.0
    del pix
    return Frame(label, ms)


def stats(frames: list[Frame]) -> tuple[float, float, float]:
    vals = sorted(f.ms for f in frames)
    mid = vals[len(vals) // 2]
    p95 = vals[min(int(len(vals) * 0.95), len(vals) - 1)]
    return vals[0], mid, p95


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="QGraphicsView 大图性能 spike")
    ap.add_argument("--input", default=None, help="真实场景 GeoTIFF（6 波段栈）")
    ap.add_argument("--out", default="output/ui_spike")
    ap.add_argument("--max-scale", type=float, default=4.0, help="最大放大倍数（超采样倍数）")
    ap.add_argument("--zoom-steps", type=int, default=12, help="放大/缩小各走几步")
    ap.add_argument("--pan-steps", type=int, default=24, help="平移走几步")
    ap.add_argument("--screenshot", action="store_true", help="存两张抓帧 PNG 作为证据")
    args = ap.parse_args()

    # ---- 找输入：不给就取 data/spike 下最新的一景 ----
    if args.input is None:
        cands = sorted((ROOT / "data" / "spike").glob("*.tif"))
        if not cands:
            print("错误：--input 未给，data/spike 下也没有场景。先跑 glof.cli fetch --save。",
                  file=sys.stderr)
            return 2
        args.input = str(cands[-1])

    # ---- Qt 平台：有桌面就用默认，起不来退 offscreen（spike 不需要真窗口）----
    app = QApplication.instance() or QApplication([])
    view = QGraphicsView()
    view.setWindowTitle("GLOF UI spike")
    view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
    view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
    view.resize(1280, 800)
    view.show()
    QApplication.processEvents()
    offscreen = app.platformName() == "offscreen"
    if offscreen:
        print(f"提示：当前在 offscreen 平台渲染（平台名 {app.platformName()}），"
              f"无合成器开销，数字略优于真机。")

    # ---- 1. 载入：读栈 → 重采样 → 拉伸 → QImage ----
    print("=" * 74)
    print("步骤 1 / 载入（读栈 + 逐波段重采样 + 拉伸 + QImage）")
    print("=" * 74)
    rss0 = rss_mb()
    t0 = time.perf_counter()
    spec = BAND_PRESETS["s2_l2a_6band"]
    scene = gio.load_scene(args.input, spec)
    t_read = time.perf_counter() - t0
    resampled = scene.resampled_bands()

    t0 = time.perf_counter()
    rgb8 = stretch_rgb(scene["red"], scene["green"], scene["blue"])
    t_stretch = time.perf_counter() - t0

    t0 = time.perf_counter()
    image = to_qimage(rgb8)
    pixmap = QPixmap.fromImage(image)
    t_image = time.perf_counter() - t0

    h, w = scene.shape
    print(f"  影像      : {Path(args.input).name}")
    print(f"  尺寸      : {w} x {h}  像元 {scene.pixel_size_m:.0f} m  "
          f"{w * h / 1e6:.1f} MP")
    if resampled:
        print(f"  重采样    : " + "  ".join(f"{k} {v:.0f}m" for k, v in resampled.items()))
    print(f"  读栈      : {t_read * 1000:.0f} ms（含 B11 20m→10m，一次性成本）")
    print(f"  拉伸      : {t_stretch * 1000:.0f} ms（2%–98%，分波段）")
    print(f"  QImage    : {t_image * 1000:.0f} ms  （含 QPixmap 上传）")
    print(f"  缓冲      : RGB888 {mb(w * h * 3)}  原始 6 波段 uint16 {mb(w * h * 6 * 2)}")

    # ---- 2. 上图 ----
    print()
    print("=" * 74)
    print("步骤 2 / 上图与缩放")
    print("=" * 74)
    gfx = QGraphicsScene()
    item = QGraphicsPixmapItem(pixmap)
    gfx.addItem(item)
    view.setScene(gfx)
    view.fitInView(gfx.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
    QApplication.processEvents()
    fit_scale = view.transform().m11()
    print(f"  fit 倍率  : {fit_scale:.2f}x（窗口 1280x800）")
    print(f"  缓冲合计  : QImage {mb(w * h * 3)} + QPixmap {mb(w * h * 4)} ≈ "
          f"{mb(w * h * 7)}（QGraphicsView 不会再拷一份）")

    frames: list[Frame] = []
    scale = fit_scale
    target = args.max_scale
    up = (target / fit_scale) ** (1.0 / args.zoom_steps)
    labels = [f"放大{i + 1}" for i in range(args.zoom_steps)]
    labels += [f"缩小{i + 1}" for i in range(args.zoom_steps)]
    factors = [up] * args.zoom_steps + [1.0 / up] * args.zoom_steps
    for label, f in zip(labels, factors):
        scale *= f
        view.resetTransform()
        view.scale(scale, scale)
        view.centerOn(item)
        frames.append(paint_once(view, label))

    lo, mid, p95 = stats(frames)
    worst = max(f.ms for f in frames)
    print(f"  缩放 {len(frames)} 帧  中位 {mid:.1f} ms   P95 {p95:.1f} ms   "
          f"最差 {worst:.1f} ms  （fit {fit_scale:.2f}x → {target:.0f}x 超采样）")

    # ---- 3. 平移（最频繁的操作，满 4x 超采样状态下拖）----
    print()
    print("=" * 74)
    print("步骤 3 / 平移（4x 超采样状态下连续拖动）")
    print("=" * 74)
    view.resetTransform()
    view.scale(target, target)
    view.centerOn(item)
    QApplication.processEvents()

    pan_frames: list[Frame] = []
    step = 120  # 像素，接近一次手拖的位移量
    for i in range(args.pan_steps):
        dx = step if i % 2 == 0 else -step
        dy = step if (i // 2) % 2 == 0 else -step
        view.scroll(dx, dy)
        pan_frames.append(paint_once(view, f"平移{i + 1}"))

    lo_p, mid_p, p95_p = stats(pan_frames)
    worst_p = max(f.ms for f in pan_frames)
    print(f"  平移 {len(pan_frames)} 帧  中位 {mid_p:.1f} ms   P95 {p95_p:.1f} ms   "
          f"最差 {worst_p:.1f} ms")

    # ---- 4. 叠加多边形的开销（风险等级着色的前景层）----
    print()
    print("=" * 74)
    print("步骤 4 / 叠加湖泊多边形后重绘")
    print("=" * 74)
    # 将来风险层的画法：QGraphicsPolygonItem + 化妆笔（0 宽，随变换保持 1px），
    # 按等级取色。这里用 200 个多边形模拟一个研究区的全部湖泊轮廓。
    overlay_pen = QPen(QColor(255, 64, 64), 0)
    overlay_pen.setCosmetic(True)
    for k in range(200):
        x0 = (k % 20) * w / 20.0
        y0 = (k // 20) * h / 10.0
        poly = QPolygonF(QRectF(x0, y0, w / 40.0, h / 40.0))
        item_poly = QGraphicsPolygonItem(poly)
        item_poly.setPen(overlay_pen)
        gfx.addItem(item_poly)
    QApplication.processEvents()
    f_overlay = paint_once(view, "叠加后")
    print(f"  叠加 200 个多边形图元后单帧 {f_overlay.ms:.1f} ms（化妆笔，无抗锯齿）")

    # ---- 5. 内存 ----
    print()
    print("=" * 74)
    print("步骤 5 / 内存（峰值 vs 释放影像栈后的稳态）")
    print("=" * 74)
    rss_peak = rss_mb()
    # 主窗口的设计就是这一步：渲染完成后立即丢掉 float 栈，只留 QImage/QPixmap。
    # 6 波段 float32 占 ~197 MB，是常驻缓冲（55 MB）的 3.6 倍，没有理由留着。
    del scene, rgb8
    gc.collect()
    rss_steady = rss_mb()
    print(f"  进程 RSS  : 启动后 {rss0:.0f} MB → 峰值 {rss_peak:.0f} MB → "
          f"释放栈后 {rss_steady:.0f} MB")
    print(f"  常驻构成  : 影像缓冲 {mb(w * h * 7)} + Python/Qt 运行时 + GDAL 块缓存")
    print(f"  说明      : GDAL 块缓存随读取增长、系统内存紧张时可回收，不算泄漏")

    # ---- 截图存证 ----
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.screenshot:
        view.resetTransform()
        view.fitInView(gfx.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        QApplication.processEvents()
        view.viewport().grab().save(str(out_dir / "spike_fit.png"))
        view.resetTransform()
        view.scale(target, target)
        view.centerOn(item)
        QApplication.processEvents()
        view.viewport().grab().save(str(out_dir / f"spike_x{target:g}.png"))
        print(f"\n  截图      : {out_dir / 'spike_fit.png'}")
        print(f"              {out_dir / f'spike_x{target:g}.png'}")

    # ---- 判定 ----
    print()
    print("=" * 74)
    print("判定")
    print("=" * 74)
    checks: list[tuple[str, bool, str]] = [
        (
            f"缩放单帧 P95 < 100 ms（滚轮离散操作红线）",
            p95 < 100.0,
            f"P95 {p95:.1f} ms，最差 {worst:.1f} ms",
        ),
        (
            f"平移单帧 P95 < 33 ms（30 fps 连续操作红线）",
            p95_p < 33.0,
            f"P95 {p95_p:.1f} ms，中位 {mid_p:.1f} ms",
        ),
        (
            "启动链路（读栈+拉伸+上图）< 5 s",
            (t_read + t_stretch + t_image) < 5.0,
            f"{(t_read + t_stretch + t_image) * 1000:.0f} ms",
        ),
        (
            "释放影像栈后稳态 RSS < 300 MB（docs §2.2 运行时预估的宽松上界）",
            rss_steady < 300.0,
            f"峰值 {rss_peak:.0f} MB（含 float 栈与 GDAL 缓存），稳态 {rss_steady:.0f} MB",
        ),
    ]
    n_pass = 0
    for text, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        n_pass += ok
        print(f"  [{mark}] {text}   {detail}")

    print()
    if n_pass == len(checks):
        print("全部通过。整景解码方案可用，主窗口不需要按视图窗口分级读取。")
    else:
        print(f"{len(checks) - n_pass} 项未过。需要评估窗口化读取 / overview 分级方案。")
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
