"""把烘好的地形纹理交给 QML 的 ``Texture { source: "image://terrain/..." }``。

和 ``image_provider.RasterImageProvider`` 同一套做法（QQuickImageProvider +
版本号换缓存键），区别是这里给 3D 材质当贴图用，所以返回 QImage 而不是
QPixmap——``Texture`` 吃 QImage 更直接，省一次 GPU 上传格式转换。
"""

from __future__ import annotations

from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

__all__ = ["TerrainTextureProvider"]


class TerrainTextureProvider(QQuickImageProvider):
    """提供当前地形纹理。QML 用 ``image://terrain/hillshade?v=N`` 取。"""

    PROVIDER_ID = "terrain"

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._image = QImage()
        self._version = 0

    def set_image(self, image: QImage) -> None:
        self._image = image

    def bump(self) -> str:
        """生成新缓存键。每换一次地形调用一次。"""
        self._version += 1
        return f"hillshade?v={self._version}"

    @property
    def url(self) -> str:
        return f"image://{self.PROVIDER_ID}/hillshade?v={self._version}"

    def requestImage(self, id_: str, size, requestedSize):  # noqa: N802 (Qt 命名)
        """QML 取图回调。Qt 约定返回的 QImage 归调用方所有，这里拷一份。"""
        return QImage(self._image)
