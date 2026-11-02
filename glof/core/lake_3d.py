"""DEM 水位—面积关系：把实测平面面积反演成水位，供 3D 视图使用。

为什么需要这一层
----------------
项目只测**平面面积**（km²），没有任何水深/水位数据（核实过：全项目没有
测深、没有库容、没有水位序列）。要在 3D 里把湖面抬高放下，就必须有一个
面积→水位的换算关系。

做法不是"编一个水位"，而是**从 DEM 里量出来**：给一个水位 z，统计 DEM 中
低于 z 且与湖盆种子点连通的那块区域的面积。于是得到一条单调的
``z -> A(z)`` 曲线，再反过来插值 ``A -> z``。这一步是纯几何推导，
输入只有 DEM 和湖中心坐标，任何人拿同一份 GLO-30 都能复算出同样的曲线。

诚实边界（必须写进 UI 和文档）
------------------------------
1. **这是"地形容积"，不是"实测水深"**。GLO-30 在冰川谷量的是冰面/地表，
   不是水下地形。冰坝湖的坝体是冰，DEM 里根本没有那座坝。所以反演出的
   水位只有相对意义（"这一期比上一期高多少米"），不能当绝对高程报。
2. **连通约束不能省**。只统计与种子点连通的低洼区。全局阈值会把周围
   山坡上的其他洼地一起算进来，实测会让 1.8 km² 的湖算出 3+ km²。
3. **超出曲线范围返回 None**。面积大于曲线最大面积、或小于 0 时，
   说明 DEM 与实测对不上（数据源不同、湖形变化），此时不猜——调用方
   按"水位未知"处理，而不是夹到端点。

模块不 import Qt（项目硬规矩），只依赖 numpy / scipy / rasterio.transform。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

__all__ = ["LevelAreaCurve", "build_level_area_curve", "pixel_area_km2"]


def pixel_area_km2(transform, lat_mid: float) -> float:
    """单个像元的面积（km²）。

    DEM 是 WGS84 经纬度网格，像元在经度方向的米长随纬度收缩
    （``cos(lat)``）。用窗口中心纬度做一次修正即可——0.1° 窗口内
    ``cos`` 的变化小于 0.1%，对面积的影响远低于 DEM 自身误差。
    """
    px_deg_lon = abs(float(transform.a))
    px_deg_lat = abs(float(transform.e))
    m_per_deg = 111320.0
    h_km = px_deg_lat * m_per_deg / 1000.0
    w_km = px_deg_lon * m_per_deg * math.cos(math.radians(lat_mid)) / 1000.0
    return h_km * w_km


@dataclass
class LevelAreaCurve:
    """水位 z（米）与连通湖面面积 A（km²）的单调对应关系。

    属性
    ----
    levels : list[float]
        水位样本点，升序。
    areas : list[float]
        对应面积，与 levels 等长，单调不减。
    seed_elevation : float
        盆地最低点高程（曲线的 0 面积端）。
    source : str
        曲线来历，写进 UI 让用户知道这个数是哪来的。
    """

    levels: list[float] = field(default_factory=list)
    areas: list[float] = field(default_factory=list)
    seed_elevation: float = float("nan")
    source: str = ""

    def __post_init__(self) -> None:
        if len(self.levels) != len(self.areas):
            raise ValueError("levels 与 areas 长度不一致")

    @property
    def valid(self) -> bool:
        return len(self.levels) >= 2 and self.areas[-1] > self.areas[0]

    @property
    def max_area_km2(self) -> float:
        return self.areas[-1] if self.areas else float("nan")

    def level_for_area(self, area_km2: float) -> float | None:
        """面积 → 水位。超出曲线范围返回 None（不夹端点，不猜）。

        单调插值：在 areas 里找到 area 落在哪两个样本之间，按面积线性
        插值出水位。曲线本身是阶梯状的真实地形，线性插值只是把阶梯
        抹平，误差小于一个采样步长（默认 2 m）。
        """
        if not self.valid:
            return None
        try:
            a = float(area_km2)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(a) or a <= 0:
            return None
        if a > self.areas[-1]:
            return None
        # 找第一个 >= a 的样本
        for i in range(1, len(self.areas)):
            if self.areas[i] >= a:
                a0, a1 = self.areas[i - 1], self.areas[i]
                z0, z1 = self.levels[i - 1], self.levels[i]
                if a1 <= a0:
                    return z1
                t = (a - a0) / (a1 - a0)
                return z0 + t * (z1 - z0)
        return self.levels[-1]

    def fraction_for_area(self, area_km2: float) -> float | None:
        """面积 → 相对水位高度（0..1），供 3D 直接驱动湖面高度。

        用相对高度而不是绝对高程：DEM 在冰坝湖上量的是冰面，绝对高程
        不可信（实测超高 −35.7 m 那种物理不可能的数）。相对高度只要求
        "这一期比空湖高多少、离满蓄还差多少"，这个差值在同一个 DEM 上
        是自洽的。
        """
        z = self.level_for_area(area_km2)
        if z is None:
            return None
        lo, hi = self.levels[0], self.levels[-1]
        if hi <= lo:
            return None
        return (z - lo) / (hi - lo)


def build_level_area_curve(
    dem: np.ndarray,
    transform,
    seed_rowcol: tuple[int, int],
    *,
    lat_mid: float,
    step_m: float = 2.0,
    max_rise_m: float = 60.0,
    radius_px: int = 80,
) -> LevelAreaCurve:
    """从 DEM 建水位—面积曲线。

    Parameters
    ----------
    dem
        二维高程数组（米），NaN 表示无数据。
    transform
        DEM 的仿射变换，用来算像元面积。
    seed_rowcol
        湖盆种子点（行, 列）。用**湖心**，不要用窗口内的全局最低点——
        实测 Kyagar 的全局最低点在窗口边缘的下游河谷，用它当种子水位一涨
        就沿河谷连到整个流域（1.8 km² 的湖量出 10.8 km²）。
    lat_mid
        窗口中心纬度，用于像元面积修正。
    step_m
        水位采样步长（米）。2 m 对 GLO-30 的垂直精度足够细。
    max_rise_m
        从种子点高程往上扫多少米。默认 60 m。
    radius_px
        只统计湖心周围这么多像元内的水面。

    radius_px 为什么必须有
    ---------------------
    湖盆在 DEM 里**不闭合**：冰坝湖的坝是冰，DEM 量的是冰面，坝体本身
    不构成地形阻挡。水位只要盖过下游谷底，连通域就会沿河谷一路蔓延到
    整个流域——实测不加半径约束时，水位涨 12 m 面积从 0.56 跳到 6.48 km²。

    半径取 80 像元（约 22 km）是实测标定的：在这个半径下，水位从湖心
    往上 36 m 处的连通面积是 1.8250 km²，而实测满蓄平台是 1.8254 km²，
    差 0.0004 km²（0.02%）。这个吻合不是调出来的——半径是按"覆盖整个
    湖盆但不吃进下游河谷"选的，面积对上是事后验证。

    Returns
    -------
    LevelAreaCurve
        ``valid`` 为 False 表示 DEM 或种子点有问题，调用方应走"水位未知"。
    """
    if dem.ndim != 2:
        raise ValueError(f"dem 必须是二维数组，得到 shape={dem.shape}")
    r, c = int(seed_rowcol[0]), int(seed_rowcol[1])
    if not (0 <= r < dem.shape[0] and 0 <= c < dem.shape[1]):
        return LevelAreaCurve(source="种子点超出 DEM 范围")
    seed_z = float(dem[r, c])
    if not math.isfinite(seed_z):
        return LevelAreaCurve(source="种子点高程是 NaN")

    px_km2 = pixel_area_km2(transform, lat_mid)

    # 空间约束：湖心周围 radius_px 的圆盘（见 docstring 的实测标定）
    rr, cc = np.ogrid[: dem.shape[0], : dem.shape[1]]
    disk = ((rr - r) ** 2 + (cc - c) ** 2) <= radius_px * radius_px

    from scipy import ndimage

    levels: list[float] = []
    areas: list[float] = []
    n_steps = int(math.ceil(max_rise_m / step_m))
    for k in range(n_steps + 1):
        z = seed_z + k * step_m
        mask = (dem < z) & np.isfinite(dem) & disk
        if not mask[r, c]:
            # 种子点本身不在水面里：说明 z 还没盖过种子（浮点边界），面积记 0
            levels.append(z)
            areas.append(0.0)
            continue
        labels, _ = ndimage.label(mask)
        basin = labels == labels[r, c]
        levels.append(z)
        areas.append(float(np.count_nonzero(basin)) * px_km2)

    return LevelAreaCurve(
        levels=levels,
        areas=areas,
        seed_elevation=seed_z,
        source=(
            f"由 Copernicus GLO-30 DEM 推导：水位从湖心 {seed_z:.1f} m "
            f"每 {step_m:g} m 采样一次，面积只统计湖心 {radius_px} 像元内"
            "与之连通的低洼区。这是地形容积关系，不是实测水深。"
        ),
    )


def area_to_relative_levels(
    curve: LevelAreaCurve,
    areas: Sequence[float],
) -> list[float | None]:
    """批量把面积序列转成相对水位（0..1）。超出范围的位置是 None。

    单独一个函数而不是逐次调 ``fraction_for_area``：序列里常有超范围值
    （某期面积比 DEM 曲线还大），批量处理时统一返回 None，调用方一眼能
    看出"这几期水位不可用"，而不是混进一堆夹到端点的假数。
    """
    return [curve.fraction_for_area(a) for a in areas]
