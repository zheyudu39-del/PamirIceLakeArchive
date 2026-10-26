"""QML 的影像图片提供器：把 QPixmap 塞给 ``Image { source: "image://raster/..." }``。

QML 的 Image 不能直接吃 Python 侧的 QPixmap，约定走 QQuickImageProvider：
Python 侧 ``set_pixmap()`` 换图，``bump()`` 换一个新缓存键（``full?v=N``），
QML 把 ``source`` 指到新键就强制重新取图——不加版本号的话，QML 的图片缓存
会认为 URL 没变而不刷新，换景后界面停留在上一景。
"""

from __future__ import annotations

from PySide6.QtGui import QPixmap
from PySide6.QtQuick import QQuickImageProvider

__all__ = ["RasterImageProvider"]


class RasterImageProvider(QQuickImageProvider):
    """提供当前景的真彩色 QPixmap。返回 Pixmap 类型：主线程同步取，无需异步管线。"""

    PROVIDER_ID = "raster"

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)
        self._pixmap = QPixmap()
        self._version = 0

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """替换当前显示的影像。QML 侧还要 ``bump()`` 换键才会重取。"""
        self._pixmap = pixmap

    def bump(self) -> str:
        """生成新缓存键 ``full?v=N``。每换一次景调用一次。"""
        self._version += 1
        return f"full?v={self._version}"

    def requestPixmap(self, id_: str, size, requestedSize):  # noqa: N802 (Qt 命名)
        """QML 取图回调。``id_`` 是 URL 里 provider 后面的部分（"full?v=N"），只透传不解析。

        Qt 约定返回的 QPixmap 归调用方所有；Qt 的隐式共享让拷贝近乎免费，
        这里拷一份，避免 QML 侧析构时动到内部缓冲。
        """
        return QPixmap(self._pixmap)
