"""栅格 → QImage/QPixmap 的显示转换（从 ui_spike 提炼，供地图与预览共用）。

真彩色显示链路：``Scene`` 的 red/green/blue（反射率 float，含 NaN 无效区）
→ 2%–98% 百分位拉伸 → uint8 (h, w, 3) → QImage → QPixmap。

两个容易翻车的细节（spike 实测，docs §2.3）：

1. 拉伸的百分位**分波段独立取**。高原雪景里红/绿波段被雪推向高值、
   蓝波段相对低，用全图统一上下界会让某一通道饱和成一片白。
2. QImage 要求每行字节数 4 字节对齐，而 ``w*3`` 不一定对齐（285*3=855）。
   不处理这条，图会歪斜或崩。做法：多分配一条对齐的缓冲，逐行只填前
   ``w*3`` 字节，``bytesPerLine`` 显式传对齐值。QImage 不拷贝外部内存，
   numpy 缓冲必须保活——挂在返回值上防 GC 后悬空指针。

NaN 处理成 0（显示为黑底）：uint8 装不下 NaN，而"无效区"在地图上就应该是
黑色，不是某个恰好可读的灰度值。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage, QPixmap

__all__ = ["stretch_rgb", "to_qimage", "scene_to_pixmap"]


def stretch_rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    """真彩色 2%–98% 百分位拉伸，输出 uint8 (h, w, 3)。

    百分位分波段独立取（只统计有限值，NaN 不参与分位数）。
    边界裁剪后线性映射到 0–255；NaN 先归 0，显示上就是黑底。
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
    """numpy uint8 (h, w, 3) → QImage（Format_RGB888，行 4 字节对齐）。

    QImage 不拷贝外部内存，返回值上挂着缓冲 ``_buf`` 保活；
    调用方不要依赖这个私有属性，只需别去 ``del`` 它。
    """
    h, w, _ = rgb8.shape
    stride = ((w * 3 + 3) // 4) * 4
    buf = np.zeros((h, stride), dtype=np.uint8)
    buf[:, : w * 3] = rgb8.reshape(h, w * 3)
    img = QImage(buf.data, w, h, stride, QImage.Format.Format_RGB888)
    img._buf = buf  # type: ignore[attr-defined]  保活，防 GC 后悬空指针
    return img


def scene_to_pixmap(scene) -> QPixmap:
    """Scene → 真彩色 QPixmap。

    只用 core 公开接口（scene["red"] 等下标访问与 scene.shape），
    不碰 Scene 内部字段——ui 层的分层约束。
    """
    rgb8 = stretch_rgb(scene["red"], scene["green"], scene["blue"])
    return QPixmap.fromImage(to_qimage(rgb8))
