"""掩膜生成：冰雪、云、地形阴影。

这一层的职责是"减法"——把不可能/不该是水体的像元提前剔掉，
让后面 water.py 的阈值判定只面对真正的候选区。

三条经验：
    * 冰雪必须用 NDSI 掩，不能用 MNDWI（两者数值相同，见 indices.py）
    * 云影和地形阴影都会让 NDWI 虚高，必须显式处理
    * 掩膜宁可保守（少掩一点），也不要激进——漏掉的水体后面还能靠人工补，
      误掩进去的雪会直接变成假湖
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "snow_mask",
    "cloud_mask_scl",
    "terrain_shadow_mask",
    "combine_masks",
]

# Sentinel-2 L2A 的 SCL（场景分类）编码
SCL_CLOUD_SHADOW = 3
SCL_CLOUD_MEDIUM = 8
SCL_CLOUD_HIGH = 9
SCL_THIN_CIRRUS = 10
SCL_SNOW = 11
SCL_WATER = 6
SCL_NODATA = 0


def snow_mask(
    ndsi: np.ndarray,
    nir: np.ndarray | None = None,
    threshold: float = 0.4,
    nir_threshold: float = 0.11,
) -> np.ndarray:
    """冰雪掩膜。

    **必须同时传 nir，否则会把湖一起掩掉。** 这一点很容易漏：

        水体  Green≈0.06, SWIR1≈0.008  -> NDSI≈0.75
        雪冰  Green≈0.88, SWIR1≈0.100  -> NDSI≈0.80

    水的 SWIR 反射率极低，所以 NDSI 同样 >0.4。只按 NDSI 掩，湖泊会整片消失。
    区分两者的是 NIR：雪在 NIR 高反射（0.6–0.9），水在 NIR 强烈吸收（<0.05）。
    这也是 Sen2Cor / Fmask 做雪检测时的标准做法——NDSI 与 NIR 双条件。

    Parameters
    ----------
    threshold
        NDSI 阈值，0.4 是 HKH 冰湖编目的常用值。
    nir_threshold
        NIR 反射率下限（0–1 口径）。0.11 是 Sentinel-2 常用的雪/水判别点。
        如果你的数据没归一化到 0–1，这个值必须同步放大。
    """
    mask = np.isfinite(ndsi) & (ndsi > threshold)
    if nir is not None:
        if nir.shape != mask.shape:
            raise ValueError(f"NIR 与 NDSI 形状不一致：{nir.shape} vs {mask.shape}")
        mask &= np.isfinite(nir) & (nir > nir_threshold)
    return mask


def cloud_mask_scl(
    scl: np.ndarray,
    *,
    include_shadow: bool = True,
    include_snow: bool = False,
) -> np.ndarray:
    """从 Sentinel-2 SCL 波段生成云/云影掩膜。

    没有 SCL 波段时，退路是用 s2cloudless 或 QA60，但那两条路都要额外依赖，
    本项目暂不引入——先把 SCL 走通。
    """
    codes = [SCL_CLOUD_MEDIUM, SCL_CLOUD_HIGH, SCL_THIN_CIRRUS]
    if include_shadow:
        codes.append(SCL_CLOUD_SHADOW)
    if include_snow:
        codes.append(SCL_SNOW)
    return np.isin(scl, codes)


def hillshade(
    dem: np.ndarray,
    pixel_size: float,
    azimuth: float = 315.0,
    altitude: float = 45.0,
) -> np.ndarray:
    """标准 ESRI 山地阴影，返回值 0–1，越小越暗。"""
    x, y = np.gradient(np.asarray(dem, dtype=np.float32), pixel_size)
    slope = np.pi / 2.0 - np.arctan(np.hypot(x, y))
    aspect = np.arctan2(-x, y)
    az = np.deg2rad(360.0 - azimuth + 90.0)
    alt = np.deg2rad(altitude)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(shaded, 0.0, 1.0)


def terrain_shadow_mask(
    dem: np.ndarray,
    pixel_size: float,
    *,
    threshold: float = 0.05,
    azimuth: float = 315.0,
    altitude: float = 45.0,
) -> np.ndarray:
    """地形阴影掩膜（hillshade 低于阈值即判为阴影）。

    注意：如果 SAR 侧用的是 ASF HyP3 的 RTC 产品，**不要用这个函数**——
    RTC 产品包里自带 Layover-Shadow mask 栅格，那是逐像元的真实几何掩膜，
    比 hillshade 阈值可靠得多。本函数只用于光学侧没有更好数据时的兜底。
    """
    return hillshade(dem, pixel_size, azimuth, altitude) < threshold


def combine_masks(*masks: np.ndarray | None) -> np.ndarray | None:
    """把若干掩膜按逻辑或合并，忽略 None。"""
    valid = [m for m in masks if m is not None]
    if not valid:
        return None
    out = np.zeros_like(valid[0], dtype=bool)
    for m in valid:
        if m.shape != out.shape:
            raise ValueError(f"掩膜形状不一致：{m.shape} vs {out.shape}")
        out |= m
    return out
