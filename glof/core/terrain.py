"""地形：坡度与坝体下游坡度——为 risk.assess_moraine_dammed() 补上唯一缺的数据源。

为什么需要这个模块
------------------
``risk.py`` 的冰碛坝判据里，``downstream_slope_deg`` 长期是 ``None``，理由写的是
"未接入 DEM"。本模块就是那个数据源：从 Copernicus GLO-30 读一个 WGS84 窗口，
算坡度，再沿坝体下游方向量 500 m 的平均坡。

已实测的事实（不是估计）
------------------------
用 Copernicus GLO-30 的 N35 E077 瓦片读克亚吉尔窗口
（77.15–77.30°E，35.60–35.75°N）：

    尺寸            540 x 540（1" 像元，0.15° / (1/3600") = 540）
    高程            4732–6709 m
    坡度中位数      27.7°（pixel_m=30.0，lat=35.675）
    >10° 的像元     79.4%

这四个数是本模块的锚点，``scripts/verify_terrain.py`` 的网络用例按它们断言。
注意中位数对应的是「每个像元自身的坡度」，不是下游坡度；单条坝体的下游坡度
会明显更缓或更陡，取决于坝址，不能拿 27.7° 当默认值。

口径
----
* 坡度用中心差分，边界像元退回单侧差分（否则整圈边界都是 NaN）。
* 经度方向的实际米数 = ``pixel_m * cos(lat)``。塔什库尔干 36°N 处 cos36°≈0.809，
  不修正会让东西向坡度系统性偏小 19%。
* 下游坡度：在 ``search_m`` 半径内按方位角取射线，选**平均高程最低**的那条，
  再沿该方向对 0–500 m 的高程剖面做最小二乘直线拟合，取 ``atan(-dz/dd)``。
  选平均高程而不是终点高程，是因为终点可能正好落在对岸山脚：一条先下到谷底
  再爬升的射线，终点能比真正的下坡方向还低，按终点选会选错方向。

缺数据就是 None
----------------
遵守项目 §8.5 与契约 §3.2：算不出来返回 ``None``，绝不返回 0。
``downstream_slope`` 在以下情况都返回 ``None``：坝体点落在 DEM 外、坝体像元是
nodata、找不到任何含有效数据的射线、500 m 剖面不完整。调用方据此走 UNKNOWN。

与 masks.py 的关系
------------------
``masks.py`` 里的 ``hillshade()`` / ``terrain_shadow_mask()`` 早已实现，但全项目
从未调用——在本模块之前，它们是死代码。本模块**不复用**它们：hillshade 产出的是
0–1 的明暗，用于光学影像的阴影像元掩膜，没有纬度修正，也不表示坡度数值；
这里要的是可解释的度数和米。两条路子不要混用。

nodata 的处理
-------------
``slope_deg`` 把非有限值（NaN/inf）视为 nodata。差分优先级是 center > 单侧，
所以 nodata 洞只让洞本身（以及两侧都无有效邻居的孤立像元）为 NaN，洞旁边
的有效像元仍能算出坡度。若数据用 -9999 之类的哨兵值表示 nodata，**调用方必须
自己先转成 NaN**——本模块不猜哨兵值，"哪个数代表 nodata" 是数据产品的属性，
藏一个默认值会悄悄改变结果。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import numpy as np
import rasterio
from affine import Affine
from rasterio.merge import merge
from rasterio.windows import from_bounds

__all__ = [
    "slope_deg",
    "downstream_slope",
    "read_dem_bounds",
    "copernicus_tile_url",
]

# GLO-30 的像元是 1 角秒。赤道约 30.9 m，35°N 的东西向约 25 m。
# 调用方仍必须显式传 pixel_m——不同 DEM 产品不一样，这里只作为参考常量。
GLO30_PIXEL_M = 30.0

# 契约 §2.2 写死的下游量测跨度。不设默认参数，因为它是判定口径的一部分。
SLOPE_SPAN_M = 500.0

# 射线采样：方位角每 10° 一条（36 条），每条在 search_m 上取 40 段。
# 这两个数只影响搜索分辨率，不影响"坡度"的定义，所以放在模块级而不是参数里。
AZIMUTH_STEP_DEG = 10.0
SEARCH_SAMPLES = 40

# 下游坡度量测：500 m 内取 6 个高程点做最小二乘拟合（0, 100, ..., 500 m）。
SPAN_SAMPLES = 6

_COPERNICUS_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """该纬度上 1° 纬差 / 1° 经差对应的米数（WGS84 一阶展开，误差 < 0.1%）。"""
    phi = math.radians(lat_deg)
    m_lat = (
        111132.92
        - 559.82 * math.cos(2.0 * phi)
        + 1.175 * math.cos(4.0 * phi)
        - 0.0023 * math.cos(6.0 * phi)
    )
    m_lon = (
        111412.84 * math.cos(phi)
        - 93.5 * math.cos(3.0 * phi)
        + 0.118 * math.cos(5.0 * phi)
    )
    return m_lat, m_lon


def _axis_gradient(z: np.ndarray, step: float, axis: int) -> np.ndarray:
    """沿一个轴做 NaN 感知的差分：中心优先，缺口处退回单侧，单侧也不成就留 NaN。

    优先级是 center > forward > backward。这样 nodata 洞只让**洞本身**是 NaN，
    洞旁边的有效像元仍能靠单侧差分得到坡度；不会因为一个洞把周围一圈都吞掉。
    只有两侧邻居都是 nodata 的孤立像元才没有可用差分，结果留 NaN。
    """
    n = z.shape[axis]

    def sl(start: int | None, stop: int | None):
        index = [slice(None)] * z.ndim
        index[axis] = slice(start, stop)
        return tuple(index)

    # d[i] = (z[i+1] - z[i]) / step，长度 n-1
    d = (z[sl(1, None)] - z[sl(None, -1)]) / step

    g = np.full(z.shape, np.nan, dtype=np.float64)
    g[sl(1, None)] = d                     # backward：g[i] = d[i-1]
    view = g[sl(0, -1)]
    fill = np.isnan(view) & np.isfinite(d)
    view[fill] = d[fill]                   # forward：g[i] = d[i]
    g[sl(0, -1)] = view

    center = (z[sl(2, None)] - z[sl(None, -2)]) / (2.0 * step)
    if n > 2:
        mid = g[sl(1, -1)]
        ok = np.isfinite(center)
        mid[ok] = center[ok]
        g[sl(1, -1)] = mid
    return g


def slope_deg(dem: np.ndarray, pixel_m: float, *, lat: float) -> np.ndarray:
    """坡度（度）。中心差分 + 纬度修正的东西向像元尺度。

    Parameters
    ----------
    dem
        二维高程数组。非有限值视为 nodata，输出在对应位置为 NaN；
        差分不会被一个空洞污染成整片 NaN。
    pixel_m
        像元边长（米）。必须显式给——不同 DEM 产品的像元不一样。
    lat
        窗口中心纬度（度），必填。经度方向的实际米数 = pixel_m * cos(lat)；
        塔什库尔干 36°N 处这个修正让东西向距离缩短约 19%，不做修正坡度会偏小。

    Returns
    -------
    np.ndarray
        与 dem 同形状的 float64 坡度（度），范围 0–90。
    """
    _require(math.isfinite(pixel_m) and pixel_m > 0, f"pixel_m 必须是正的有限数，收到 {pixel_m!r}")
    _require(math.isfinite(lat) and abs(lat) < 90.0, f"lat 必须在 (-90, 90) 内，收到 {lat!r}")

    z = np.asarray(dem, dtype=np.float64)
    _require(z.ndim == 2, f"dem 必须是二维数组，收到 {z.ndim} 维")
    _require(
        z.shape[0] >= 2 and z.shape[1] >= 2,
        f"dem 至少要有 2x2 个像元才能算差分，收到 {z.shape}",
    )

    dx = pixel_m * math.cos(math.radians(lat))
    _require(dx > 0.0, f"lat={lat!r} 处东西向像元尺度为 0，无法计算坡度")

    gx = _axis_gradient(z, dx, axis=1)
    gy = _axis_gradient(z, pixel_m, axis=0)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    # 差分已经保证 nodata 处为 NaN；这里再兜一次 inf，避免 inf 混进统计。
    slope[~np.isfinite(z)] = np.nan
    return slope


# ---------------------------------------------------------------------------
# 下游坡度
# ---------------------------------------------------------------------------


def _sample(dem: np.ndarray, inv: Affine, lon: float, lat: float) -> float:
    """双线性采样；四邻域里有 nodata 时按剩余有效权重重归一。"""
    col, row = inv * (lon, lat)
    # 仿射矩阵的 (lon, lat) 是像元角点坐标，减 0.5 到像元中心。
    col -= 0.5
    row -= 0.5
    h, w = dem.shape
    if not (math.isfinite(row) and math.isfinite(col)):
        return math.nan
    r = math.floor(row)
    c = math.floor(col)
    # 边界判定要按**像元中心坐标**做，不能只看左上角像元下标。
    # 有效区间是 [-0.5, h-0.5]（最后一格的半个像元仍可插值），
    # 写成 floor(row) >= h 会把 h-0.5 这种「恰好在边缘内侧」的点也放进来，
    # 随后被 min(r0+1, h-1) 夹成同一个像元、重复加权，等于凭外推造出一个
    # 数值——实测下游坡度因此返回 14.05° 而不是 None。
    if row < -0.5 or col < -0.5 or row > h - 0.5 or col > w - 0.5:
        return math.nan
    r0 = int(r)
    c0 = int(c)
    r1 = min(r0 + 1, h - 1)
    c1 = min(c0 + 1, w - 1)
    fr = row - r0
    fc = col - c0
    values = np.array(
        [dem[r0, c0], dem[r0, c1], dem[r1, c0], dem[r1, c1]], dtype=np.float64
    )
    weights = np.array(
        [(1 - fr) * (1 - fc), (1 - fr) * fc, fr * (1 - fc), fr * fc],
        dtype=np.float64,
    )
    valid = np.isfinite(values)
    if not valid.any():
        return math.nan
    wsum = float(weights[valid].sum())
    if wsum <= 0.0:
        return math.nan
    return float((values[valid] * weights[valid]).sum() / wsum)


def _offset(lat: float, lon: float, azimuth_deg: float, dist_m: float) -> tuple[float, float]:
    """沿方位角（0=北，90=东）走 dist_m 米后的 (lon, lat)。"""
    m_lat, m_lon = _meters_per_degree(lat)
    az = math.radians(azimuth_deg)
    return lon + dist_m * math.sin(az) / m_lon, lat + dist_m * math.cos(az) / m_lat


def downstream_slope(
    dem: np.ndarray,
    transform: Affine,
    dam_lat: float,
    dam_lon: float,
    *,
    search_m: float = 2000.0,
) -> float | None:
    """坝体下游坡度（度）。

    做法：从坝体点出发，在 search_m 半径内按方位角取射线，选平均高程最低的
    那条；再沿该方向对 0–500 m 的高程剖面做直线拟合，取平均坡度。

    选平均高程而不是终点高程，是因为终点可能落在对岸山脚：一条先下到谷底
    再爬升的射线，终点能比真正的下坡方向还低。

    完整跑完全程的射线优先于中途出界的射线：中途出界时越界段是 NaN，
    剩余有限值的均值偏高，按均值直接比会选错方向。

    返回 ``None`` 表示无法计算：坝体点不在 DEM 内、坝体像元是 nodata、
    所有射线都没有有效数据、或 500 m 剖面不完整。调用方据此走 UNKNOWN，
    **不要**把 None 当成 0。注意返回值可以是负数：如果 search_m 内没有任何
    下降方向（坝体在局部洼地），量出来的就是上坡，这是事实，不该抹成 0。

    Parameters
    ----------
    dem
        二维高程数组（米）。
    transform
        地理坐标系（度）的仿射变换，与 dem 对应。
    dam_lat, dam_lon
        坝体位置（WGS84 经纬度）。
    search_m
        找下坡方向的最大半径（米）。
    """
    _require(
        math.isfinite(search_m) and search_m > 0,
        f"search_m 必须是正的有限数，收到 {search_m!r}",
    )
    _require(
        math.isfinite(dam_lat) and math.isfinite(dam_lon),
        f"坝体经纬度必须是有限数，收到 ({dam_lat!r}, {dam_lon!r})",
    )
    z = np.asarray(dem, dtype=np.float64)
    _require(z.ndim == 2, f"dem 必须是二维数组，收到 {z.ndim} 维")
    _require(z.size > 0, "dem 不能为空")
    _require(isinstance(transform, Affine), f"transform 必须是 affine.Affine，收到 {type(transform)!r}")
    _require(transform.a != 0 and transform.e != 0, "transform 的像元尺度不能为 0")

    inv = ~transform
    z0 = _sample(z, inv, dam_lon, dam_lat)
    if not math.isfinite(z0):
        # 坝体点落在 DEM 外或落在 nodata 上：没有可量测的起点。
        return None

    distances = np.linspace(0.0, float(search_m), SEARCH_SAMPLES + 1)
    # 只比较**每条射线都走得通**的那一段距离。
    #
    # 早先按"有限值均值"挑方向，隐含前提是各射线长度相同。射线跑出 DEM
    # 时后半段变成 NaN，剩下的有限值均值天然偏高，于是真正一路下到边界外
    # 的方向反而输给一条完整但只斜着下降的射线——实测把 14.04° 的斜坡
    # 量成 10.85°。反过来"完整优先"也不行：它会把越界的那条（真实下坡）
    # 直接判负。两种排法都错在**拿不同长度的剖面比高低**。
    #
    # 正确做法：先扫一遍求出所有射线共同有效的最大距离 d_max，
    # 再只用 0..d_max 这段比较。d_max 不足 SLOPE_SPAN_M 时无法量 500 m
    # 坡度，直接返回 None（不拿半条剖面凑数）。
    valid_mask = np.zeros((int(round(360.0 / AZIMUTH_STEP_DEG)), distances.size),
                          dtype=bool)
    for k in range(valid_mask.shape[0]):
        az = k * AZIMUTH_STEP_DEG
        for j, d in enumerate(distances):
            valid_mask[k, j] = math.isfinite(
                _sample(z, inv, *_offset(dam_lat, dam_lon, az, d)))
    if not valid_mask.any():
        return None
    common = valid_mask.all(axis=0)          # 每个距离上是否所有射线都有效
    # 从近到远找第一个失效点；它之前的距离是所有射线共同的量程
    bad = np.flatnonzero(~common)
    common_max_m = float(distances[bad[0] - 1]) if bad.size else float(distances[-1])
    if common_max_m < SLOPE_SPAN_M:
        # 连 500 m 都凑不齐，量不出坡度的就是量不出
        return None

    use = distances <= common_max_m
    best_az: float | None = None
    best_mean = math.inf
    for k in range(valid_mask.shape[0]):
        az = k * AZIMUTH_STEP_DEG
        profile = np.array(
            [_sample(z, inv, *_offset(dam_lat, dam_lon, az, d)) for d in distances[use]],
            dtype=np.float64,
        )
        if not np.isfinite(profile).all():
            continue
        mean_z = float(profile.mean())
        if mean_z < best_mean:
            best_mean = mean_z
            best_az = az
    if best_az is None:
        return None

    # 沿选中方向量 500 m 平均坡度。剖面必须完整，否则宁可返回 None。
    span = np.linspace(0.0, SLOPE_SPAN_M, SPAN_SAMPLES)
    heights = np.array(
        [_sample(z, inv, *_offset(dam_lat, dam_lon, best_az, d)) for d in span],
        dtype=np.float64,
    )
    if not np.isfinite(heights).all():
        return None
    # 最小二乘拟合 h = a + b*d，下游坡度为 atan(-b)。
    b = float(np.polyfit(span, heights, 1)[0])
    return math.degrees(math.atan(-b))


# ---------------------------------------------------------------------------
# DEM 读取
# ---------------------------------------------------------------------------


def copernicus_tile_url(lat: float, lon: float) -> str:
    """包含 (lat, lon) 的 Copernicus GLO-30 1° 瓦片 URL。

    瓦片名以西南角整度命名，南北半球用 N/S、东西半球用 E/W，**取绝对值**：

        (35.7, 77.2)   -> N35_00_E077_00   （覆盖 35–36N, 77–78E）
        (-1.2, -70.5)  -> S02_00_W071_00   （覆盖 2S–1S, 71W–70W）

    负值必须向下取整：-1.2 属于 S02（-2..-1），不是 S01。直接 int() 会截断成 -1，
    把瓦片名字错一整度，所以这里统一用 floor。
    """
    _require(math.isfinite(lat) and math.isfinite(lon), f"经纬度必须是有限数，收到 ({lat!r}, {lon!r})")
    lat_i = math.floor(lat)
    lon_i = math.floor(lon)
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    name = f"Copernicus_DSM_COG_10_{ns}{abs(lat_i):02d}_00_{ew}{abs(lon_i):03d}_00_DEM"
    return f"{_COPERNICUS_BASE}/{name}/{name}.tif"


def _validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    _require(len(bbox) == 4, f"bbox 必须是 (lon_min, lat_min, lon_max, lat_max)，收到 {bbox!r}")
    lon_min, lat_min, lon_max, lat_max = (float(v) for v in bbox)
    _require(
        all(math.isfinite(v) for v in (lon_min, lat_min, lon_max, lat_max)),
        f"bbox 含非有限值：{bbox!r}",
    )
    _require(lon_min < lon_max, f"bbox 经度顺序颠倒或为零跨度：{bbox!r}")
    _require(lat_min < lat_max, f"bbox 纬度顺序颠倒或为零跨度：{bbox!r}")
    _require(-180.0 <= lon_min and lon_max <= 180.0, f"bbox 经度超出 [-180, 180]：{bbox!r}")
    _require(-90.0 <= lat_min and lat_max <= 90.0, f"bbox 纬度超出 [-90, 90]：{bbox!r}")
    return lon_min, lat_min, lon_max, lat_max


def _copernicus_tiles_for_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """覆盖 bbox 的全部 1° 瓦片。上界用 nextafter 避免正好压在整度线上的空瓦片。"""
    lon_min, lat_min, lon_max, lat_max = bbox
    lat_a = math.floor(lat_min)
    lat_b = math.floor(math.nextafter(lat_max, -math.inf))
    lon_a = math.floor(lon_min)
    lon_b = math.floor(math.nextafter(lon_max, -math.inf))
    return [
        copernicus_tile_url(la + 0.5, lo + 0.5)
        for la in range(lat_a, lat_b + 1)
        for lo in range(lon_a, lon_b + 1)
    ]


def read_dem_bounds(
    url_or_path: str | Path | None,
    bbox: tuple[float, float, float, float],
) -> tuple[np.ndarray, Affine]:
    """读取 WGS84 bbox 窗口，返回 ``(dem, transform)``。

    ``url_or_path=None`` 时按 bbox 自动拼 Copernicus GLO-30 瓦片 URL 并拼接。
    GLO-30 的 COG 支持 HTTP range 读，不需要认证，也不需要先下载整幅
    （N35 E077 单瓦片 37 MB，读 540x540 窗口只取几十 KB）。

    bbox 是 ``(lon_min, lat_min, lon_max, lat_max)``，与 accuracy.py 的
    ``scene_bounds`` 同口径。给本地路径时若 bbox 超出文件范围，按交集读并
    返回**裁剪后**的 transform——调用方必须用返回的 transform，不能继续用原图的。

    nodata 一律转成 NaN。若数据用哨兵值且文件里没写 nodata，本函数不会猜。
    """
    lon_min, lat_min, lon_max, lat_max = _validate_bbox(bbox)
    bounds = (lon_min, lat_min, lon_max, lat_max)

    if url_or_path is None:
        sources = []
        try:
            for url in _copernicus_tiles_for_bbox(bounds):
                sources.append(rasterio.open(url))
            merged, transform = merge(sources, bounds=bounds)
            nodata = sources[0].nodata
        finally:
            for src in sources:
                src.close()
        dem = merged[0].astype(np.float64)
        if nodata is not None:
            dem[dem == nodata] = np.nan
        dem[~np.isfinite(dem)] = np.nan
        return dem, transform

    with rasterio.open(url_or_path) as src:
        if src.crs is not None and not src.crs.is_geographic:
            raise ValueError(
                f"bbox 是 WGS84 经纬度，但 {url_or_path} 的坐标系是 {src.crs}；"
                "请先用 rasterio.warp 重投影，或直接传经纬度网格"
            )
        left = max(lon_min, src.bounds.left)
        right = min(lon_max, src.bounds.right)
        bottom = max(lat_min, src.bounds.bottom)
        top = min(lat_max, src.bounds.top)
        if left >= right or bottom >= top:
            raise ValueError(f"bbox {bounds} 与 {url_or_path} 的范围 {tuple(src.bounds)} 没有重叠")
        window = from_bounds(left, bottom, right, top, transform=src.transform)
        data = src.read(1, window=window, masked=True)
        dem = np.ma.filled(data.astype(np.float64), np.nan)
        transform = src.window_transform(window)
    return dem, transform
