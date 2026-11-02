"""栅格读取与结果落盘。

真实 Sentinel-2 L2A 的波段分辨率并不统一：

    B02 / B03 / B04 / B08   10 m
    B11 / B12               20 m
    B01 / B09 / B10         60 m

所以本模块的核心是**把不同分辨率的波段重采样到同一个参考网格**。
做法：以最细分辨率的波段为参考网格（通常是 10 m 的 B08），
其余波段在 `read()` 时用 `out_shape` 一次性重采样过去，不落地中间文件。

单文件与多文件都支持：

    # 单文件：所有波段在同一个 GeoTIFF 里（合成影像、已对齐的堆栈）
    load_scene("scene.tif", S2_L2A_6BAND)

    # 多文件：每个波段一个文件，分辨率可以不同（真实 S2 L2A）
    load_scene({"green": "B03.tif", "nir": "B08.tif", "swir1": "B11.tif"}, ...)

多文件时每个文件默认读第 1 波段；同文件多波段可传 (路径, 波段号)。

**几何假设**（Sentinel-2 L2A 成立，其他数据需自行确认）：
各波段同 CRS、同覆盖范围、网格成整数倍嵌套（20 m 网格正好是 10 m 网格每 2 个像元取 1）。
不满足时本模块只做整体缩放重采样，不做真正的几何配准——那种情况请先用 GDAL 对齐。
"""

from __future__ import annotations

import csv
import json
import os
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.windows import Window

from .indices import BandSpec

__all__ = [
    "Scene",
    "load_scene",
    "save_stack",
    "describe_raster",
    "save_geojson",
    "load_lakes_geojson",
    "save_csv",
]

_BAND_NAMES = ("blue", "green", "red", "nir", "swir1", "swir2")


def _is_remote(source: str | Path) -> bool:
    """是否是远程 URL（http/https/s3 等）。

    只用来决定"要不要并发读"：远程各波段是独立资源、并发有收益；
    本地文件并发无收益且会争抢 GDAL 块缓存。
    """
    text = os.fspath(source)
    return text.startswith(("http://", "https://", "s3://", "gs://", "az://"))

BandSource = str | Path | tuple[str | Path, int]


def _display_name(location: str | Path) -> str:
    """给报错用的短名字。远程 URL 不能丢给 Path()——Windows 上会把
    ``https://a/b`` 变成 ``https:\\a\\b``，导致 GDAL 报 Bad hostname。"""
    text = str(location)
    if "://" in text:
        return text.rsplit("/", 1)[-1][:60]
    return Path(text).name


@dataclass
class Scene:
    """一景已重采样到统一网格的反射率数据。"""

    path: str | Path
    data: dict[str, np.ndarray]
    transform: Affine
    crs: Any
    reflectance_scale: float
    profile: dict[str, Any] = field(default_factory=dict)
    band_resolutions: dict[str, float] = field(default_factory=dict)
    target_resolution: float = 0.0
    sources: dict[str, str | Path] = field(default_factory=dict)

    def __getitem__(self, name: str) -> np.ndarray:
        return self.data[name]

    def has(self, *names: str) -> bool:
        return all(n in self.data for n in names)

    @property
    def shape(self) -> tuple[int, int]:
        return next(iter(self.data.values())).shape

    @property
    def pixel_area_m2(self) -> float:
        """单个像元面积（平方米）。带旋转的仿射变换也成立。"""
        a, b, d, e = self.transform.a, self.transform.b, self.transform.d, self.transform.e
        return abs(a * e - b * d)

    @property
    def pixel_size_m(self) -> float:
        return float(abs(self.transform.a))

    def resampled_bands(self) -> dict[str, float]:
        """返回被重采样过的波段及其**原始**分辨率。空字典表示全部原生同网格。"""
        return {
            name: res
            for name, res in self.band_resolutions.items()
            if abs(res - self.target_resolution) > 1e-6
        }


def _auto_scale(dtype: np.dtype) -> float:
    """按 dtype 猜反射率缩放因子。

    Sentinel-2 L2A 的 uint16 是 DN×10000；本项目统一按 10000 处理，
    并在 CLI 暴露 --scale 供手工覆盖。
    """
    if np.issubdtype(dtype, np.floating):
        return 1.0
    if dtype == np.uint8:
        return 255.0
    return 10000.0


def _normalize_sources(source: Any, spec: BandSpec) -> tuple[dict[str, tuple[str | Path, int]], set[str]]:
    """把入参统一成 {波段名: (位置, 波段号)}，并返回用户显式指定过波段号的集合。

    单文件 -> 用 spec 里声明的波段号（一个文件装 6 个波段）
    多文件 -> 默认每个文件第 1 波段，可用 (位置, 波段号) 覆盖

    位置可以是本地路径，也可以是 https:// 开头的远程 COG——
    后者**绝对不能**包成 Path，否则 Windows 会把 :// 改写成 :\\ 。
    """
    needed = [name for name in _BAND_NAMES if getattr(spec, name) is not None]

    if isinstance(source, (str, Path)):
        return {name: (source, getattr(spec, name)) for name in needed}, set(needed)

    if not isinstance(source, Mapping):
        raise TypeError(f"source 必须是路径或 {{波段名: 路径}} 映射，收到 {type(source)!r}")

    out: dict[str, tuple[str | Path, int]] = {}
    explicit: set[str] = set()
    for name in needed:
        if name not in source:
            # 映射**可以只给一部分波段**：远程读一景的成本与波段数成正比
            # （见 docs/时序检索为什么慢.md），时序流程只用得上 3 个。
            # 缺的波段就是不读，而不是报错——但至少要有一个，否则下面
            # 选参考网格时会 IndexError。
            continue
        value = source[name]
        if isinstance(value, (tuple, list)):
            out[name] = (value[0], int(value[1]))
            explicit.add(name)
        else:
            out[name] = (value, 1)

    if not out:
        raise ValueError(
            f"映射里没有可用波段。spec {spec.name} 支持 {needed}，"
            f"收到 {sorted(source)}。至少给一个。"
        )
    return out, explicit


def _window_from_bounds(
    bounds: Sequence[float], transform: Affine, crs: Any, max_w: int, max_h: int
) -> Window:
    """把 WGS84 经纬度范围换成参考网格上的窗口，并裁到影像边界内。"""
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    projected = transform_bounds("EPSG:4326", crs, *[float(v) for v in bounds])
    win = from_bounds(*projected, transform=transform).round_offsets().round_lengths()

    col0 = max(0, int(win.col_off))
    row0 = max(0, int(win.row_off))
    col1 = min(max_w, int(win.col_off + win.width))
    row1 = min(max_h, int(win.row_off + win.height))
    if col1 <= col0 or row1 <= row0:
        raise ValueError(
            f"bounds {tuple(bounds)} 完全落在影像范围之外。"
            f" 先跑 `glof.cli info` 看这景的实际覆盖范围。"
        )
    return Window(col0, row0, col1 - col0, row1 - row0)


def load_scene(
    source: Any,
    spec: BandSpec,
    *,
    reflectance_scale: float | str = "auto",
    ref_resolution: float | None = None,
    window: Window | None = None,
    bounds: Sequence[float] | None = None,
    scale_factor: int = 1,
    resampling: Resampling = Resampling.bilinear,
    http_headers: Mapping[str, str] | None = None,
) -> Scene:
    """读取一景影像，把各波段重采样到统一参考网格并换算成反射率。

    Parameters
    ----------
    source
        单文件路径，或 ``{波段名: 路径}`` / ``{波段名: (路径, 波段号)}`` 映射。
    ref_resolution
        参考网格的像元大小（CRS 单位，通常米）。默认取所有波段中最细的原生分辨率，
        即 Sentinel-2 的 10 m。想统一到 20 m 分析就传 20。
    window
        **参考网格坐标系下**的读取窗口。None 表示整景。
    bounds
        ``(lon_min, lat_min, lon_max, lat_max)``，WGS84 经纬度，自动换算成参考网格窗口。
        与 ``window`` 二选一。接真实数据时用它比手算窗口省事得多。
    scale_factor
        在参考网格基础上再做整数倍降采样，仅供预览调试。
        正式统计面积必须保持 1，否则 pixel_area 与实际不符、面积会成倍偏小。
    resampling
        重采样方法。连续量（反射率）用 bilinear；分类量（SCL）应传 nearest。
    http_headers
        远程 COG 的 HTTP 头。Planetary Computer 用 SAS 签名不需要；
        CDSE 的 Bearer 认证需要传 ``{"Authorization": "Bearer ..."}``。
    """
    band_sources, explicit = _normalize_sources(source, spec)

    env: dict[str, Any] = {}
    if http_headers:
        env["GDAL_HTTP_HEADERS"] = "\r\n".join(f"{k}: {v}" for k, v in http_headers.items())

    with rasterio.Env(**env):
        handles: dict[str, rasterio.DatasetReader] = {}
        # 同一路径的多个波段共用一个句柄：GDAL 的块缓存是挂在 Dataset 上的，
        # 像素交错（interleave=pixel）的 GeoTIFF 每读一个波段都要解压整块。
        # 各波段各开一个句柄时这份缓存互不共享，同一块被解压 N 次——
        # 实测 spike 场景（6 波段、LZW、像素交错）从 3887 ms 降到 826 ms。
        by_path: dict[str, rasterio.DatasetReader] = {}
        try:
            for name, (path, bidx) in band_sources.items():
                key = os.fspath(path)
                ds = by_path.get(key)
                if ds is None:
                    ds = rasterio.open(path)
                    by_path[key] = ds
                handles[name] = ds
                if bidx < 1 or bidx > ds.count:
                    raise ValueError(
                        f"{_display_name(path)} 只有 {ds.count} 个波段，但 {name} 要读第 {bidx} 波段。"
                    )
                if name not in explicit and ds.count > 1:
                    warnings.warn(
                        f"波段 {name} 读的是 {_display_name(path)} 的第 1 波段，"
                        f"但该文件有 {ds.count} 个波段。"
                        f" 若想指定波段，请传 (路径, 波段号) 而不是只给路径。",
                        RuntimeWarning,
                        stacklevel=2,
                    )

            # ---- 1. 确定参考网格 ----
            crs = handles[next(iter(handles))].crs
            for name, ds in handles.items():
                if ds.crs != crs:
                    raise ValueError(
                        f"波段 {name} 的坐标系 {ds.crs} 与其它波段 {crs} 不一致，"
                        f"请先统一投影。"
                    )

            resolutions = {
                name: float(abs(ds.transform.a)) for name, ds in handles.items()
            }
            min_res = min(resolutions.values())
            target_res = float(ref_resolution) if ref_resolution else min_res

            ref_name = min(resolutions, key=lambda k: resolutions[k])
            ref_ds = handles[ref_name]

            # 覆盖范围一致性检查：S2 各波段范围相同，偏移超过 2 个最粗像元就要提醒
            max_res = max(resolutions.values())
            tol = 2.0 * max_res
            ref_bounds = ref_ds.bounds
            for name, ds in handles.items():
                b = ds.bounds
                offset = max(
                    abs(b.left - ref_bounds.left),
                    abs(b.bottom - ref_bounds.bottom),
                    abs(b.right - ref_bounds.right),
                    abs(b.top - ref_bounds.top),
                )
                if offset > tol:
                    warnings.warn(
                        f"波段 {name} 的覆盖范围与参考波段 {ref_name} 相差 {offset:.1f} m"
                        f"（容差 {tol:.1f} m）。本模块只做整体缩放，不做几何配准，"
                        f"结果可能错位。请先用 GDAL 对齐。",
                        RuntimeWarning,
                        stacklevel=2,
                    )

            # ---- 2. 参考网格尺寸与变换 ----
            factor = min_res / target_res  # 参考波段原生像元 / 目标像元
            ref_h = max(1, int(round(ref_ds.height * factor)))
            ref_w = max(1, int(round(ref_ds.width * factor)))
            ref_transform = ref_ds.transform * Affine.scale(1.0 / factor, 1.0 / factor)

            win = window if window is not None else Window(0, 0, ref_w, ref_h)
            if bounds is not None:
                if window is not None:
                    raise ValueError("window 与 bounds 只能给一个")
                win = _window_from_bounds(bounds, ref_transform, crs, ref_w, ref_h)
            out_h = max(1, int(win.height // scale_factor))
            out_w = max(1, int(win.width // scale_factor))

            scale = (
                _auto_scale(np.dtype(handles[next(iter(handles))].dtypes[0]))
                if reflectance_scale == "auto"
                else float(reflectance_scale)
            )

            # ---- 3. 逐波段读入并重采样 ----
            #
            # 远程 COG 时各波段是**独立的 HTTP 资源**（PC 的 S2 每景 13 个文件），
            # 没法共享连接。串行读实测 9.4 s/景，并发后压到最慢那个的量级
            # （见 docs/时序检索为什么慢.md）。本地文件保持串行——并发读同一
            # 个文件没有收益，还会让 GDAL 块缓存互相踩。
            def read_band(name: str):
                ds = handles[name]
                _, bidx = band_sources[name]
                src_px_per_target = target_res / resolutions[name]
                src_win = Window(
                    win.col_off * src_px_per_target,
                    win.row_off * src_px_per_target,
                    win.width * src_px_per_target,
                    win.height * src_px_per_target,
                )
                arr = ds.read(
                    bidx,
                    window=src_win,
                    out_shape=(out_h, out_w),
                    resampling=resampling,
                    masked=True,
                )
                return (arr.astype(np.float32) / scale).filled(np.nan)

            names = list(handles)
            data: dict[str, np.ndarray] = {}
            # 并发的前提：各波段是**不同**的远程资源。同一个 Dataset 句柄不支持
            # 多线程并发 read（GDAL 的 dataset 不是线程安全的），所以只要有两个
            # 波段共用一个句柄就必须退回串行。
            distinct_handles = len({id(ds) for ds in handles.values()}) == len(names)
            can_parallel = (
                len(names) > 1
                and distinct_handles
                and all(_is_remote(band_sources[n][0]) for n in names)
            )
            if can_parallel:
                # max_workers 取波段数与 8 的较小值：S2 最多 6 个波段，
                # 再高也不会更快，只是把请求压在同一个域上更容易被限流。
                with ThreadPoolExecutor(max_workers=min(len(names), 8)) as pool:
                    futures = {n: pool.submit(read_band, n) for n in names}
                    for n in names:
                        data[n] = futures[n].result()   # 异常原样抛出
            else:
                for n in names:
                    data[n] = read_band(n)

            transform = (
                ref_transform
                * Affine.translation(win.col_off, win.row_off)
                * Affine.scale(win.width / out_w, win.height / out_h)
            )

            return Scene(
                path=band_sources[next(iter(band_sources))][0],
                data=data,
                transform=transform,
                crs=crs,
                reflectance_scale=scale,
                profile=ref_ds.profile.copy(),
                band_resolutions=resolutions,
                target_resolution=target_res,
                sources={name: p for name, (p, _) in band_sources.items()},
            )
        finally:
            # by_path 里是去重后的句柄；handles 里同一句柄可能出现多次，
            # 按它关闭会重复 close（GDAL 会报错），所以只关 by_path。
            for ds in by_path.values():
                ds.close()


def save_stack(scene: Scene, path: str | Path, *, scale: float = 10000.0) -> Path:
    """把已读入的 Scene 落成本地 6 波段 GeoTIFF。

    存成 uint16（DN = 反射率 × 10000），与 Sentinel-2 原生约定一致，
    `load_scene()` 能自动识别并按 /10000 还原，文件体积只有 float32 的一半。

    **这是"一次联网、之后离线"的关键**：抓一次真实数据存到 data/raw/，
    后面所有调试都不再依赖网络。
    """
    order = [name for name in _BAND_NAMES if name in scene.data]
    h, w = scene.shape
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        out,
        "w",
        driver="GTiff",
        height=h,
        width=w,
        count=len(order),
        dtype="uint16",
        crs=scene.crs,
        transform=scene.transform,
        nodata=0,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as dst:
        for idx, name in enumerate(order, start=1):
            arr = np.nan_to_num(scene[name], nan=0.0)
            dst.write(np.clip(np.rint(arr * scale), 0, 65535).astype("uint16"), idx)
            dst.set_band_description(idx, name)
    return out


def describe_raster(path: str | Path) -> dict[str, Any]:
    """给 `glof.cli info` 用的元数据摘要。"""
    with rasterio.open(Path(path)) as src:
        return {
            "path": str(path),
            "driver": src.driver,
            "size": f"{src.width} x {src.height}",
            "count": src.count,
            "dtype": src.dtypes[0],
            "crs": src.crs.to_string() if src.crs else None,
            "pixel_size": (round(abs(src.transform.a), 4), round(abs(src.transform.e), 4)),
            "nodata": src.nodata,
            "bounds": tuple(round(v, 6) for v in src.bounds),
        }


def save_geojson(records: Iterable[Any], path: str | Path) -> Path:
    """写出湖泊多边形，坐标系 WGS84（GeoJSON 规范要求）。"""
    features = []
    for rec in records:
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(rec.geometry_wgs84_json),
                "properties": {
                    "lake_id": rec.lake_id,
                    "area_km2": round(rec.area_km2, 6),
                    "perimeter_km": round(rec.perimeter_m / 1000.0, 4),
                    "mean_ndwi": round(rec.mean_ndwi, 4),
                    "n_pixels": rec.n_pixels,
                    "centroid_lon": round(rec.centroid_lon, 6),
                    "centroid_lat": round(rec.centroid_lat, 6),
                    # 风险字段跟着几何走：地图上点开一个湖就能看到判据原文，
                    # 不用再去对照另一张表。reasons 保留为字符串列表，方便前端逐条渲染。
                    "risk_level": rec.risk_level,
                    "risk_reasons": list(rec.risk_reasons),
                },
            }
        )

    payload = {"type": "FeatureCollection", "features": features}
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_lakes_geojson(path: str | Path) -> list:
    """读回 ``save_geojson()`` 写出的湖泊 GeoJSON，返回 ``LakeRecord`` 列表。

    对称操作。存在的理由很实际：``exposure`` / ``report`` 这些下游命令
    应该能吃上一次提取的结果，而不是每件事都重新读一遍影像——重读一景
    联网要 9–30 秒，而结果早就落盘了。

    缺字段时的默认值刻意取"没量到"那一侧：
    ``area_km2`` 缺失 -> ``area_m2 = nan``（不是 0），``risk_level`` 缺失
    -> ``"UNKNOWN"``（不是 ``"LOW"``）。这与全项目「缺数据返回 None」一致。
    """
    from .water import LakeRecord

    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    features = doc.get("features", []) if isinstance(doc, dict) else []
    out: list[LakeRecord] = []
    for idx, feat in enumerate(features, start=1):
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if not geom:
            # 没有几何的记录无法画图，但面积/质心可能仍有意义；
            # 保留它并让 geometry 为空串，由下游决定要不要跳过。
            geom_json = ""
        else:
            geom_json = json.dumps(geom)
        area_km2 = props.get("area_km2")
        area_m2 = float("nan") if area_km2 is None else float(area_km2) * 1e6
        perim_km = props.get("perimeter_km")
        out.append(
            LakeRecord(
                lake_id=int(props.get("lake_id", idx)),
                area_m2=area_m2,
                perimeter_m=float("nan") if perim_km is None else float(perim_km) * 1000.0,
                n_pixels=int(props.get("n_pixels", 0)),
                area_px_m2=area_m2,
                mean_ndwi=float(props.get("mean_ndwi", float("nan"))),
                centroid_lon=float(props.get("centroid_lon", float("nan"))),
                centroid_lat=float(props.get("centroid_lat", float("nan"))),
                geometry_wgs84_json=geom_json,
                risk_level=str(props.get("risk_level", "UNKNOWN")),
                risk_reasons=list(props.get("risk_reasons") or []),
            )
        )
    return out


CSV_FIELDS = [
    "lake_id",
    "area_km2",
    "area_ha",
    "perimeter_km",
    "mean_ndwi",
    "n_pixels",
    "centroid_lon",
    "centroid_lat",
    "risk_level",
    "risk_reasons",
]


def save_csv(records: Iterable[Any], path: str | Path) -> Path:
    """写出属性表。用 utf-8-sig，Excel 直接双击不会乱码。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = list(records)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for rec in rows:
            writer.writerow(
                {
                    "lake_id": rec.lake_id,
                    "area_km2": round(rec.area_km2, 6),
                    "area_ha": round(rec.area_km2 * 100.0, 3),
                    "perimeter_km": round(rec.perimeter_m / 1000.0, 4),
                    "mean_ndwi": round(rec.mean_ndwi, 4),
                    "n_pixels": rec.n_pixels,
                    "centroid_lon": round(rec.centroid_lon, 6),
                    "centroid_lat": round(rec.centroid_lat, 6),
                    # CSV 只能放平铺文本，判据用 " | " 拼起来，Excel 里一行仍看得完
                    "risk_level": rec.risk_level,
                    "risk_reasons": " | ".join(rec.risk_reasons),
                }
            )
    return out
