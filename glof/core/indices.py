"""光谱指数与波段约定。

--------------------------------------------------------------------------
为什么主用 NDWI，而不是 MNDWI
--------------------------------------------------------------------------
把两个公式并排放：

    MNDWI = (Green - SWIR1) / (Green + SWIR1)      Xu, 2006
    NDSI  = (Green - SWIR1) / (Green + SWIR1)      Salomonson & Appel, 2004

**它们是同一个公式。** 在 Sentinel-2 上，Green=B03、SWIR1=B11，两个指数的
数值逐像元完全相等，只是判读阈值不同（水体 MNDWI>0，冰雪 NDSI>0.4）。

由此可以推出一条硬结论：

    "用 MNDWI 提水，再用 NDSI>0.4 掩掉冰雪" 这两步是自相矛盾的。
    因为它们输出的是同一个数——凡是被 NDSI 判为雪(>0.4)的像元，
    MNDWI 也必然 >0.4，早已经被当成水提取出来了。掩膜要么把水一起掩掉，
    要么什么也没掩住。

高原冰湖场景下，雪/冰在 SWIR 反射率很低（新雪约 0.05–0.15），
Green 又很高，所以雪会稳定地被 MNDWI 判成水。这就是"MNDWI 对雪冰更稳"
这一说法反了的原因。

因此本模块：

    提水   -> ndwi()   (Green - NIR) / (Green + NIR)     <- 主力
    掩冰雪 -> ndsi()   (Green - SWIR1) / (Green + SWIR1) <- 只做掩膜
    对照   -> mndwi()  仅为消融实验保留，内部直接调用 ndsi()

水体在 NIR 波段强烈吸收，而雪在 NIR 仍然高反射（0.6–0.9），
所以 NDWI 才是能把两者分开的那个指数。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "BandSpec",
    "S2_L2A_4BAND",
    "S2_L2A_6BAND",
    "LANDSAT_8_9",
    "BAND_PRESETS",
    "normalized_difference",
    "ndwi",
    "ndsi",
    "mndwi",
    "awei_sh",
]


# --------------------------------------------------------------------------
# 波段约定
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BandSpec:
    """描述一个多波段栅格里各波段的位置（**1 起始**，与 rasterio 一致）。

    GeoTIFF 本身不携带"第几个波段是 Green"这种信息，所以必须由使用者显式声明。
    用错波段是本项目最常见、也最难发现的错误——NDWI 算出来一堆噪声，
    却看不出是波段错位，只会以为阈值没调好。

    所有波段字段都允许为空：提水只需要 green/red/nir，SWIR 只在算 NDSI 时用到。
    """

    name: str
    blue: int | None = None
    green: int | None = None
    red: int | None = None
    nir: int | None = None
    swir1: int | None = None
    swir2: int | None = None

    def __post_init__(self) -> None:
        for field in ("green", "red", "nir"):
            if getattr(self, field) is None:
                raise ValueError(f"{self.name} 缺少必需波段 {field}")
        for field in ("blue", "green", "red", "nir", "swir1", "swir2"):
            value = getattr(self, field)
            if value is not None and value < 1:
                raise ValueError(f"{self.name}.{field} 必须从 1 开始计数，收到 {value}")

    @property
    def has_swir(self) -> bool:
        """没有 SWIR 就算不出 NDSI，冰雪掩膜这一步会直接缺失。"""
        return self.swir1 is not None

    def required_bands(self) -> list[int]:
        return [
            b
            for b in (self.blue, self.green, self.red, self.nir, self.swir1, self.swir2)
            if b
        ]

    def max_band(self) -> int:
        return max(self.required_bands())


# Sentinel-2 L2A，只取 10 m 四波段（B02,B03,B04,B08）。
# 注意：这条路走不通 NDSI——B11 是 20 m，不在这套堆栈里。
S2_L2A_4BAND = BandSpec("s2_l2a_4band", blue=1, green=2, red=3, nir=4)

# Sentinel-2 L2A，B02,B03,B04,B08,B11,B12 重采样到同一网格（推荐）。
# 注意 B11/B12 原生 20 m，重采样到 10 m 只是插值，不是真的提高分辨率。
S2_L2A_6BAND = BandSpec("s2_l2a_6band", blue=1, green=2, red=3, nir=4, swir1=5, swir2=6)

# Landsat 8/9 L2 地表反射率，SR_B2..SR_B7。
LANDSAT_8_9 = BandSpec("landsat_8_9", blue=2, green=3, red=4, nir=5, swir1=6, swir2=7)

BAND_PRESETS: dict[str, BandSpec] = {
    spec.name: spec
    for spec in (S2_L2A_4BAND, S2_L2A_6BAND, LANDSAT_8_9)
}


# --------------------------------------------------------------------------
# 指数计算
# --------------------------------------------------------------------------


def normalized_difference(a: np.ndarray, b: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    """(a - b) / (a + b)，分母接近 0 时返回 NaN。

    返回 NaN 而不是 0 或 inf 是刻意的：后续所有掩膜都用 np.isfinite 判有效性，
    NaN 会自然地把"无数据/除零"排除在统计之外，不会污染均值。
    """
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.shape != b.shape:
        raise ValueError(f"两个波段形状不一致：{a.shape} vs {b.shape}")

    denom = a + b
    out = np.full(a.shape, np.nan, dtype=np.float32)
    ok = np.abs(denom) > eps
    out[ok] = (a[ok] - b[ok]) / denom[ok]
    return out


def ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """NDWI（McFeeters 1996）。**本项目提水的主力指数。**

    水体在 NIR 强烈吸收 -> NIR 低 -> NDWI 高（典型 0.3–0.8）。
    雪冰在 NIR 仍高反射 -> NDWI 接近 0 或为负。
    这一条差异，正是 NDWI 能在高原把冰湖和雪分开的原因。
    """
    return normalized_difference(green, nir)


def ndsi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """NDSI（Salomonson & Appel 2004）。**只用于生成冰雪掩膜，不用于提水。**

    雪在 SWIR1 强吸收 -> NDSI 高（典型 0.6–0.95）；水体 SWIR 也低，
    但水的 Green 同样低，比值没有雪那么极端，实践中靠阈值 + 后续掩膜区分。
    """
    return normalized_difference(green, swir1)


def mndwi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """MNDWI（Xu 2006）。

    .. warning::
        与 ndsi() **数值完全相同**。保留此函数只为消融实验与文献对照，
        不要用它做主提取链路，更不要与 NDSI 掩膜串联使用——理由见模块文档。
    """
    return ndsi(green, swir1)


def awei_sh(
    green: np.ndarray,
    nir: np.ndarray,
    swir1: np.ndarray,
    swir2: np.ndarray,
) -> np.ndarray:
    """AWEIsh（Feyisa et al. 2014），无阴影水指数，作为 NDWI 的交叉验证。

    公式：4*(Green - SWIR1) - (0.25*NIR + 2.75*SWIR2)，判水阈值通常取 0。
    """
    green = np.asarray(green, dtype=np.float32)
    nir = np.asarray(nir, dtype=np.float32)
    swir1 = np.asarray(swir1, dtype=np.float32)
    swir2 = np.asarray(swir2, dtype=np.float32)
    return 4.0 * (green - swir1) - (0.25 * nir + 2.75 * swir2)
