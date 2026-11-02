"""把 DEM 烘成带山体阴影的纹理图，供 3D 地形模型当 baseColorMap 用。

为什么走纹理而不是顶点属性
--------------------------
实测（Qt 6.12 + software 后端）：``ProceduralMesh`` 的 ``normals`` 和
``colors`` 两个属性在运行时 metaObject 里都存在，赋值也不报错，但画面
**逐像素不变**——有法线和没法线两张截图完全一致。软件渲染后端不消费
这些顶点属性。

纹理走的是另一条管线：``PrincipledMaterial.baseColorMap`` 是标准 PBR
贴图路径，软件后端一定支持（``probe_3d.png`` 里的 Cube/Sphere 就是靠
材质正常上色的）。所以把光照烘进一张图，比跟顶点属性较劲可靠。

烘的是什么
----------
标准山体阴影（hillshade）：按坡度和坡向算一个"朝光程度"，映射到明暗。
不是物理光照，是制图学里的地形晕渲——好处是结果确定、可复现，
同一份 DEM 每次烘出来一样。

色带用冰川/岩石的灰绿，湖盆低处偏深，高处偏亮，符合高原地貌观感。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage

__all__ = ["render_hillshade", "make_terrain_image"]


def render_hillshade(
    dem: np.ndarray,
    *,
    azimuth_deg: float = 315.0,
    altitude_deg: float = 45.0,
    z_factor: float = 3.0,
) -> np.ndarray:
    """标准山体阴影，返回 0..1 的亮度数组。

    Parameters
    ----------
    dem
        二维高程数组（米）。NaN 会被填成最小值。
    azimuth_deg
        光源方位角（度），315 是制图惯例（西北方向打光，符合北半球直觉）。
    altitude_deg
        光源高度角（度）。45 是常用值。
    z_factor
        垂直夸张系数。1.0 是真实比例；这里默认 3.0，因为 GLO-30 的
        水平分辨率 30 m 对垂直变化偏"平"，不夸张一点看不出沟谷。

    算法
    ----
    经典 hillshade（Horn 1981）：
        坡度 slope = atan(z_factor * sqrt(dzdx² + dzdy²) / cellsize)
        坡向 aspect = atan2(dzdy, -dzdx)
        亮度 = sin(alt) * cos(slope) + cos(alt) * sin(slope) * cos(az - aspect)
    再归一化到 0..1。
    """
    z = np.array(dem, dtype=np.float64)
    finite = np.isfinite(z)
    if not np.any(finite):
        return np.full(z.shape, 0.5)
    z = np.where(finite, z, np.nanmin(z))

    # 像元在经度方向按中纬度收缩；这里只用于坡度量级，取一个常数近似
    cellsize = 30.0

    dzdy, dzdx = np.gradient(z, cellsize)
    dzdx *= z_factor
    dzdy *= z_factor

    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)

    az = np.radians(azimuth_deg)
    alt = np.radians(altitude_deg)

    shaded = (np.sin(alt) * np.cos(slope)
              + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    # 映射到 0..1：原始范围约 -1..1，压到 0.15..1 保留环境光
    shaded = np.clip(shaded, -1.0, 1.0)
    return 0.15 + 0.85 * (shaded + 1.0) / 2.0


def make_terrain_image(
    dem: np.ndarray,
    *,
    z_factor: float = 3.0,
    azimuth_deg: float = 315.0,
    altitude_deg: float = 45.0,
) -> QImage:
    """DEM → 带山体阴影的 QImage（RGB888）。

    配色：低处（湖盆/谷底）偏深青灰，高处（雪线以上）偏亮灰白。
    用高程分位做插值，而不是固定高程阈值——不同湖的绝对高程差很大，
    固定阈值在另一个湖上就失效了。
    """
    z = np.array(dem, dtype=np.float64)
    finite = np.isfinite(z)
    if not np.any(finite):
        z = np.zeros_like(z)
    else:
        z = np.where(finite, z, np.nanmin(z))

    shade = render_hillshade(z, azimuth_deg=azimuth_deg,
                             altitude_deg=altitude_deg, z_factor=z_factor)

    # 高程分位归一化（2%..98%，避开极值）
    lo, hi = np.percentile(z, [2.0, 98.0])
    span = max(1e-6, hi - lo)
    t = np.clip((z - lo) / span, 0.0, 1.0)

    # 三段色带：谷底深青 → 山坡灰绿 → 高处浅灰（雪线感）
    r = 0.16 + t * 0.62
    g = 0.26 + t * 0.56
    b = 0.24 + t * 0.58

    r = np.clip(r * shade, 0.0, 1.0)
    g = np.clip(g * shade, 0.0, 1.0)
    b = np.clip(b * shade, 0.0, 1.0)

    rgb = np.stack([(r * 255).astype(np.uint8),
                    (g * 255).astype(np.uint8),
                    (b * 255).astype(np.uint8)], axis=-1)
    rgb = np.ascontiguousarray(rgb)

    h, w = rgb.shape[:2]
    img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    # QImage 不持有 numpy 缓冲，必须 copy——否则 numpy 数组回收后图像变黑
    return img.copy()
