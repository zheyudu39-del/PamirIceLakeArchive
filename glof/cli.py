"""无界面命令行入口。

前期所有验证都走这里，界面一行都不写。用法：

    # 看一景影像的元数据（先确认波段数、坐标系、像元大小）
    python -m glof.cli info --input data/raw/scene.tif

    # 提取湖泊
    python -m glof.cli extract --input data/raw/scene.tif --out output/run1

    # 自动阈值 + 关掉冰雪掩膜（做消融对比用）
    python -m glof.cli extract --input data/raw/scene.tif --out output/run2 \\
        --threshold otsu --no-snow-mask

    # 对面积时序逐期做风险分级（留出法估平台，阈值可覆盖、可做敏感性）
    python -m glof.cli risk --input output/kyagar/kyagar_area_series.csv \\
        --lake-type ice --sensitivity --out output/risk

    # 最新可用观测（不是"实时"：影像有拍摄日期，会明确告诉你是几天前的）
    python -m glof.cli latest --bbox 77.16 35.65 77.22 35.71 --days 60
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from pathlib import Path

import numpy as np

from .core import io as gio
from .core import masks
from .core import stac as gstac
from .core.indices import BAND_PRESETS, ndsi, ndwi
from .core.water import extract_lakes, summarize


def _pad(text: str, width: int, align: str = "left") -> str:
    """按终端显示宽度补空格——中文占两格，用 len() 会错位。"""
    display = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    gap = max(0, width - display)
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def _fmt(value: float, digits: int = 4) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def cmd_info(args: argparse.Namespace) -> int:
    info = gio.describe_raster(args.input)
    width = max(len(k) for k in info)
    for key, value in info.items():
        print(f"  {_pad(key, width)} : {value}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    spec = BAND_PRESETS[args.bands]
    scene = gio.load_scene(
        args.input,
        spec,
        reflectance_scale=args.scale if args.scale != "auto" else "auto",
        scale_factor=args.downsample,
    )

    print(f"影像      : {Path(args.input).name}")
    print(f"尺寸      : {scene.shape[1]} x {scene.shape[0]}  像元 {scene.pixel_size_m:.1f} m")
    print(f"坐标系    : {scene.crs}")
    resampled = scene.resampled_bands()
    if resampled:
        desc = "  ".join(
            f"{name} {res:.0f}m->{scene.target_resolution:.0f}m" for name, res in resampled.items()
        )
        print(f"重采样    : {desc}")
    else:
        print(f"重采样    : 无（各波段原生 {scene.target_resolution:.0f} m）")
    print(f"反射率缩放: /{scene.reflectance_scale:g}")
    print(f"波段预设  : {spec.name}")

    ndwi_arr = ndwi(scene["green"], scene["nir"])
    finite = np.isfinite(ndwi_arr)
    if finite.sum() == 0:
        print("错误：NDWI 全为无效值，检查波段是否读对。", file=sys.stderr)
        return 2
    print(
        f"NDWI      : 有效 {finite.sum():,} 像元   "
        f"均值 {np.nanmean(ndwi_arr):.3f}   最大 {np.nanmax(ndwi_arr):.3f}"
    )

    exclude = None
    if not args.no_snow_mask:
        if spec.has_swir:
            ndsi_arr = ndsi(scene["green"], scene["swir1"])
            # 必须带 NIR 双条件：水的 NDSI 同样 >0.4，只按 NDSI 掩会把湖整片掩掉
            snow = masks.snow_mask(ndsi_arr, scene["nir"], args.ndsi_threshold)
            exclude = masks.combine_masks(exclude, snow)
            print(f"冰雪掩膜  : NDSI > {args.ndsi_threshold} 且 NIR > 0.11   "
                  f"剔除 {snow.sum():,} 像元 ({snow.mean() * 100:.1f}%)")
        else:
            print(
                f"警告：预设 {spec.name} 没有 SWIR 波段，无法生成 NDSI 冰雪掩膜。"
                f" 高原场景下这会导致雪被误判为水。",
                file=sys.stderr,
            )

    if args.threshold == "otsu":
        from .core.water import otsu_threshold

        thr = otsu_threshold(ndwi_arr)
        print(f"阈值      : Otsu = {thr:.4f}")
    else:
        thr = float(args.threshold)
        print(f"阈值      : NDWI > {thr}")

    records = extract_lakes(
        ndwi_arr,
        scene.transform,
        scene.crs,
        threshold=thr,
        min_area_km2=args.min_area_km2,
        exclude=exclude,
        close_radius_px=args.close_px,
        fill_holes=not args.no_fill_holes,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not records:
        print("\n未提取到满足条件的湖泊。可尝试降低 --min-area-km2 或 --threshold。")
        return 1

    if args.save_mask:
        import rasterio

        from .core.water import build_water_mask

        mask = build_water_mask(
            ndwi_arr,
            threshold=thr,
            exclude=exclude,
            close_radius_px=args.close_px,
            fill_holes=not args.no_fill_holes,
        ).astype(np.uint8)
        mask_path = out_dir / "water_mask.tif"
        with rasterio.open(
            mask_path,
            "w",
            driver="GTiff",
            height=mask.shape[0],
            width=mask.shape[1],
            count=1,
            dtype="uint8",
            crs=scene.crs,
            transform=scene.transform,
            compress="lzw",
        ) as dst:
            dst.write(mask, 1)
        print(f"掩膜      : {mask_path}")

    shown = records[: args.top]
    header = [
        ("ID", 4, "right"),
        ("面积 km²", 10, "right"),
        ("周长 km", 10, "right"),
        ("平均NDWI", 10, "right"),
        ("像元数", 9, "right"),
        ("质心经度", 11, "right"),
        ("质心纬度", 11, "right"),
    ]
    print()
    print("  ".join(_pad(name, w, a) for name, w, a in header))
    print("  ".join("-" * w for _, w, _ in header))
    for rec in shown:
        print(
            "  ".join(
                [
                    _pad(str(rec.lake_id), 4, "right"),
                    _pad(_fmt(rec.area_km2), 10, "right"),
                    _pad(_fmt(rec.perimeter_m / 1000.0), 10, "right"),
                    _pad(_fmt(rec.mean_ndwi), 10, "right"),
                    _pad(f"{rec.n_pixels:,}", 9, "right"),
                    _pad(_fmt(rec.centroid_lon, 5), 11, "right"),
                    _pad(_fmt(rec.centroid_lat, 5), 11, "right"),
                ]
            )
        )
    if len(records) > args.top:
        print(f"  ... 另有 {len(records) - args.top} 个未显示")

    stats = summarize(records)
    print()
    print(
        f"合计 {stats['n_lakes']} 个水体，总面积 {stats['total_area_km2']:.4f} km²"
        f"（最大 {stats['max_area_km2']:.4f}，最小 {stats['min_area_km2']:.4f} km²）"
    )

    geojson = gio.save_geojson(records, out_dir / "lakes.geojson")
    csv_path = gio.save_csv(records, out_dir / "lakes.csv")
    print(f"输出      : {geojson}")
    print(f"            {csv_path}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """检索 Sentinel-2，可选把 AOI 窗口落成本地 GeoTIFF。"""
    bbox = [float(v) for v in args.bbox]
    print(f"研究区    : lon {bbox[0]}–{bbox[2]}  lat {bbox[1]}–{bbox[3]}")
    print(f"时间      : {args.start} → {args.end}   云量 < {args.max_cloud}%")
    print(f"数据源    : {args.endpoint}")

    refs = gstac.search_sentinel2(
        bbox,
        args.start,
        args.end,
        max_cloud=args.max_cloud,
        limit=args.limit,
        endpoint=args.endpoint,
        platform=args.platform,
    )
    if not refs:
        print("\n没有命中的影像。放宽 --max-cloud 或扩大时间范围再试。", file=sys.stderr)
        return 1

    cols = [
        ("#", 3, "right"),
        ("瓦片", 7, "left"),
        ("日期", 12, "left"),
        ("云量%", 7, "right"),
        ("景 ID", 44, "left"),
    ]
    print()
    print("  " + " ".join(_pad(n, w, a) for n, w, a in cols))
    print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))
    for idx, ref in enumerate(refs, start=1):
        print(
            "  "
            + " ".join(
                [
                    _pad(str(idx), 3, "right"),
                    _pad(ref.tile, 7),
                    _pad(ref.date, 12),
                    _pad(f"{ref.cloud_cover:.1f}", 7, "right"),
                    _pad(ref.id, 44),
                ]
            )
        )
    print(f"\n共 {len(refs)} 景（按云量升序）。云量是整景统计，AOI 内仍可能有云。")

    if not args.save:
        print("\n加 --save 可把 AOI 窗口落成本地 GeoTIFF，之后离线跑 extract。")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for ref in refs[: args.save]:
        print(f"\n[{ref.tile} {ref.date}] 签名 + 读取 AOI 窗口 ...")
        try:
            sources, headers = gstac.sign_scene(ref, bearer_token=args.token)
            scene = gio.load_scene(
                sources,
                BAND_PRESETS[args.bands],
                bounds=bbox,
                http_headers=headers or None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        path = gio.save_stack(scene, out_dir / f"{ref.id}.tif")
        size_mb = path.stat().st_size / 1024**2
        print(
            f"  已存 {path}  {scene.shape[1]}x{scene.shape[0]}  "
            f"{scene.pixel_size_m:.0f} m  {size_mb:.1f} MB"
        )
        saved.append({"id": ref.id, "tile": ref.tile, "date": ref.date,
                      "cloud": ref.cloud_cover, "file": str(path)})

    if saved:
        manifest = out_dir / "scenes.json"
        manifest.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n清单      : {manifest}")
        print(f"下一步    : python -m glof.cli extract --input {saved[0]['file']} --out output/real1")
    return 0


def cmd_latest(args: argparse.Namespace) -> int:
    """打印研究区的「最新可用观测」。

    措辞上的三条硬规矩（见 docs/最新观测契约.md）：
    * 一律叫「最新可用观测」，不叫"实时"——影像有拍摄日期，必须显示距今几天；
    * ``fetch_latest`` 返回 None 是「没有可用影像」，不是「没有湖」；
    * ``total_area_km2`` 为 None 是「没量到」，绝不能显示成 0。
    """
    from .core.latest import FROZEN_NDWI_MAX, fetch_latest
    from .core.timeseries import NDWI_THRESHOLD, NIR_MAX

    bbox = [float(v) for v in args.bbox]

    def progress(msg: str) -> None:
        # 进度固定走 stderr：--json 时 stdout 只能有那一段 JSON
        print(f"  {msg}", file=sys.stderr)

    obs = fetch_latest(
        bbox,
        lookback_days=args.days,
        max_cloud=args.max_cloud,
        min_area_km2=args.min_area_km2,
        progress=progress,
    )

    # --json 时人读的附注改走 stderr，别污染 stdout
    def note(text: str) -> None:
        print(text, file=sys.stderr if args.json else sys.stdout)

    if obs is None:
        message = (
            f"最近 {args.days} 天内没有可用观测（云量上限 {args.max_cloud:g}%）。"
            f"这不是\"没有湖\"，是没有可用影像。"
        )
        if args.json:
            print(json.dumps(
                {
                    "observed_on": None,
                    "age_days": None,
                    "scene_id": None,
                    "tile": None,
                    "cloud": None,
                    "coverage": None,
                    "n_lakes": None,
                    "total_area_km2": None,
                    "largest_area_km2": None,
                    "ndwi_max": None,
                    "season_warning": None,
                    "message": message,
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print("最新可用观测")
            print(f"  {message}")
            print("  可试：放宽 --max-cloud、加大 --days，或换更小/更靠谷底的 --bbox。")
        return 1

    ndwi_max = None if not np.isfinite(obs.ndwi_max) else round(obs.ndwi_max, 4)

    if args.json:
        print(json.dumps(
            {
                "observed_on": obs.observed_on,
                "age_days": obs.age_days,
                "scene_id": obs.scene_id,
                "tile": obs.tile,
                "cloud": round(obs.cloud, 2),
                "coverage": round(obs.coverage, 4),
                "n_lakes": obs.n_lakes,
                # None 必须原样保留成 null，不能兜底成 0
                "total_area_km2": (
                    None if obs.total_area_km2 is None else round(obs.total_area_km2, 4)
                ),
                "largest_area_km2": (
                    None if obs.largest_area_km2 is None else round(obs.largest_area_km2, 4)
                ),
                "ndwi_max": ndwi_max,
                "season_warning": obs.season_warning,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("最新可用观测")
        print(f"  {_pad('观测日期', 10)}{obs.observed_on}（距今 {obs.age_days} 天）")
        print(f"  {_pad('云量', 10)}{obs.cloud:.1f}%   覆盖率 {obs.coverage:.0%}")
        print(f"  {_pad('瓦片', 10)}{obs.tile}")
        print(f"  {_pad('场景', 10)}{obs.scene_id}")

        if obs.total_area_km2 is None:
            # 没量到 ≠ 面积为 0。但"没量到"有两种成因，措辞必须分开——
            # 把 NDWI 最大值说成"低于门限"而它其实高于门限，就是在撒谎。
            ndwi_txt = "无效" if ndwi_max is None else f"{ndwi_max:.3f}"
            if ndwi_max is None or ndwi_max < FROZEN_NDWI_MAX:
                why = f"低于 {FROZEN_NDWI_MAX:g}，说明没有水面信号"
            else:
                why = (f"高于 {FROZEN_NDWI_MAX:g}，但按判水口径"
                       f"（NDWI>{NDWI_THRESHOLD:g} 且 NIR<{NIR_MAX:g}）"
                       f"没有提出可信水体")
            print(f"  {_pad('面积', 10)}没量到。窗口内 NDWI 最大值 {ndwi_txt}，{why}。")
            print(f"  {_pad('说明', 10)}这是「没量到」，不是「面积为零」；"
                  f"冰封或无水面信号时面积不可用。")
        else:
            largest = (
                "没量到" if obs.largest_area_km2 is None
                else f"{obs.largest_area_km2:.2f} km²"
            )
            print(f"  {_pad('湖泊', 10)}{obs.n_lakes} 个，"
                  f"总面积 {obs.total_area_km2:.2f} km²，最大 {largest}")

        # 量到了也要提示非消融期；没量到时同样要说明原因
        if obs.season_warning:
            print(f"  {_pad('提示', 10)}{obs.season_warning}")

    if args.save:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        if obs.records:
            geo = gio.save_geojson(obs.records, out_dir / "lakes.geojson")
            csv_path = gio.save_csv(obs.records, out_dir / "lakes.csv")
            note(f"输出      : {geo}")
            note(f"            {csv_path}")
        else:
            note("没有可落盘的湖泊：本条观测没量到水体，不是「湖是空的」。")
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    """从 ICIMOD 的 HMA GLOF 数据库里筛出研究区的溃决湖。"""
    from .core import inventory as ginv

    events, source = ginv.load_glof_database(args.db)
    print(f"数据源    : {source}")
    print(f"总事件    : {len(events)}")
    print(f"研究区    : lon {args.bbox[0]}–{args.bbox[2]}  lat {args.bbox[1]}–{args.bbox[3]}"
          + (f"   国家 {args.country}" if args.country else ""))

    selected = ginv.filter_bbox(events, args.bbox, country=args.country)
    if not selected:
        print("\n研究区内没有记录。换 bbox 或去掉 --country 再试。", file=sys.stderr)
        return 1

    sites = ginv.cluster_lakes(selected, radius_m=args.cluster_m)
    print(f"命中事件  : {len(selected)}  去重后湖 : {len(sites)}")

    cols = [("湖名", 14, "left"), ("纬度", 10, "right"), ("经度", 11, "right"),
            ("事件数", 7, "right"), ("起止年", 12, "left"), ("海拔", 7, "right"),
            ("坝型", 13, "left"), ("流域", 11, "left")]
    print()
    print("  " + " ".join(_pad(n, w, a) for n, w, a in cols))
    print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))
    for s in sites:
        span = f"{s.first_year or '?'}-{s.last_year or '?'}"
        print("  " + " ".join([
            _pad(s.name[:12], 14),
            _pad(f"{s.lat:.4f}", 10, "right"),
            _pad(f"{s.lon:.4f}", 11, "right"),
            _pad(str(s.n_events), 7, "right"),
            _pad(span, 12),
            _pad(f"{s.elev:.0f}" if s.elev else "-", 7, "right"),
            _pad(s.lake_type[:11], 13),
            _pad(s.basin[:9], 11),
        ]))

    out = Path(args.out)
    geo = ginv.save_sites_geojson(sites, out / "glof_sites.geojson")
    csv_path = out / "glof_sites.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "lat", "lon", "n_events", "first_year", "last_year",
                    "elev_m", "lake_type", "country", "basin", "total_lives"])
        for s in sites:
            w.writerow([s.name, f"{s.lat:.6f}", f"{s.lon:.6f}", s.n_events,
                        s.first_year, s.last_year, s.elev, s.lake_type,
                        s.country, s.basin, s.total_lives])
    print()
    print(f"输出      : {geo}")
    print(f"            {csv_path}")
    print(f"引用      : {ginv.CITATION}")
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    """对面积时序逐期做溃决风险判定（留出法估平台，无权重）。"""
    from .core import risk as grisk
    from .core import risk_series as grs

    overrides: dict[str, float] = {}
    try:
        for item in args.threshold or []:
            name, sep, value = item.partition("=")
            if not sep:
                raise KeyError(f"阈值要写成 名字=数值，收到 {item!r}")
            overrides[name.strip()] = float(value)
        unknown = set(overrides) - set(grisk.DEFAULT_THRESHOLDS)
        if unknown:
            raise KeyError(f"未知阈值名 {sorted(unknown)}")
    except (KeyError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print(f"可用阈值：{sorted(grisk.DEFAULT_THRESHOLDS)}", file=sys.stderr)
        return 2

    try:
        points = grs.read_series_csv(args.input)
    except (OSError, ValueError) as exc:
        print(f"错误：读取时序失败：{exc}", file=sys.stderr)
        return 2
    if not points:
        print(f"错误：{args.input} 里没有可用观测行。", file=sys.stderr)
        return 1
    span = f"{points[0].date} → {points[-1].date}"
    print(f"时序      : {args.input}  共 {len(points)} 期（{span}）")
    print(f"坝型      : {args.lake_type}  阈值覆盖: {overrides or '无（全部用默认）'}")

    # --dem 给了就从 DEM 量坝体下游坡度，覆盖 --slope-deg。
    # 量不出来（None）时保留原值——若原值也是 None，判据照旧走 UNKNOWN。
    slope = args.slope_deg
    dem_lat, dem_lon = args.dem_lat, args.dem_lon

    # --dam-position：从湖边界最低点自动估坝体，替掉"拿湖心当坝体"。
    # 湖心是错的——实测克亚吉尔湖心四周 500 m 只有 +10~+250 m 岸坡，
    # 量出 -0.01°；真实坡地可达 52°。坝体在湖的下游出口，不在几何中心。
    if args.dam_position:
        if not args.dem:
            print("错误：--dam-position 需要 --dem。", file=sys.stderr)
            return 2
        if not args.lakes:
            print("错误：--dam-position 需要 --lakes（湖泊 GeoJSON，"
                  "坝体是从湖边界几何估的）。", file=sys.stderr)
            return 2
        from .core import dam_position as gdam
        from .core import terrain as gterr

        records = gio.load_lakes_geojson(args.lakes)
        if not records:
            print(f"{args.lakes} 里没有湖泊，无法估坝体位置。", file=sys.stderr)
            return 2
        # 取面积最大的那个湖估坝体——风险判定针对主湖
        main = max(records, key=lambda r: (r.area_m2 if r.area_m2 == r.area_m2 else -1))
        # 主湖面积和时序里的最大面积差太多，说明 --lakes 拿错了景/窗口。
        # 这不是小概率：--lakes 若是另一个 bbox 的提取结果，坝体会被估到
        # 另一个湖的出口，坡度判据整个失去意义。宁可报错也不要静默算下去。
        series_max = max((p.area_km2 for p in points if p.area_km2 == p.area_km2),
                         default=None)
        main_km2 = main.area_m2 / 1e6 if main.area_m2 == main.area_m2 else None
        if series_max and main_km2 and (main_km2 < series_max * 0.2 or main_km2 > series_max * 5):
            print(f"错误：--lakes 里的主湖 {main_km2:.4f} km² 与面积时序的最大值 "
                  f"{series_max:.4f} km² 差了一个量级。", file=sys.stderr)
            print(f"  这多半是 --lakes 用了另一景/另一个 bbox 的提取结果。"
                  f"坝体会被估到别的湖出口，坡度判据就失去意义了。", file=sys.stderr)
            return 2
        bbox = (main.centroid_lon - 0.05, main.centroid_lat - 0.05,
                main.centroid_lon + 0.05, main.centroid_lat + 0.05)
        try:
            dem_arr, dem_tr = gterr.read_dem_bounds(args.dem, bbox)
        except Exception as exc:  # noqa: BLE001 - 读不到 DEM 就退回手工坐标
            print(f"DEM       : 读不到 {Path(args.dem).name}（{type(exc).__name__}），"
                  f"无法自动估坝体", file=sys.stderr)
            return 2
        est = gdam.dam_position(
            main.geometry_wgs84_json, dem_arr, dem_tr, "EPSG:4326")
        if est is None:
            print(f"坝体位置  : 估不出来（DEM 无有效高程），该判据保持 UNKNOWN",
                  file=sys.stderr)
        else:
            print(f"坝体位置  : {est.lon:.5f}, {est.lat:.5f}"
                  f"（边界最低点，高程 {est.elevation_m:.1f} m，"
                  f"下游方位 {est.azimuth_deg:.0f}°，边界起伏 {est.boundary_relief_m:.0f} m）")
            print(f"            湖心 {main.centroid_lon:.5f}, {main.centroid_lat:.5f}"
                  f" 只作对照——坝体在出口，不在几何中心")
            dem_lat, dem_lon = est.lat, est.lon
            # 坝体位置已定，坡度直接用 dam_position 的采样，避免重复读 DEM
            slope_from_dam = gterr.downstream_slope(dem_arr, dem_tr, est.lat, est.lon)
            if slope_from_dam is not None:
                slope = slope_from_dam
                print(f"下游坡度  : {slope:.2f}°（坝体位置量得）")

    if args.dem and not args.dam_position and (dem_lat is None or dem_lon is None):
        print("错误：--dem 必须配 --dem-lat 与 --dem-lon（坝体位置），"
              "或改用 --dam-position 自动估。", file=sys.stderr)
        return 2
    if args.dem and not args.dam_position:
        measured = grs.slope_from_dem(
            args.dem, float(dem_lat), float(dem_lon))
        if measured is None:
            print(f"DEM       : {Path(args.dem).name} 取不到可量测的坝体下游坡度，"
                  f"该判据保持 UNKNOWN")
        else:
            print(f"DEM       : {Path(args.dem).name} 量得坝体下游坡度 "
                  f"{measured:.2f}°（坡心近似坝体，非精确坝址）")
            slope = measured

    try:
        results = grs.assess_series(
            points,
            args.lake_type,
            date=args.date,
            baseline_date=args.baseline_date,
            downstream_slope_deg=slope,
            glacier_contact={"yes": True, "no": False}.get(args.glacier_contact),
            thresholds=overrides or None,
            min_valid_frac=args.min_valid_frac,
        )
    except KeyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.sensitivity:
        cases = grs.series_sensitivity_cases(
            points,
            args.lake_type,
            downstream_slope_deg=slope,
            glacier_contact={"yes": True, "no": False}.get(args.glacier_contact),
        )
        if cases:
            print()
            print("=" * 74)
            print(f"阈值 ±20% 敏感性（{len(cases)} 个用例）")
            print("=" * 74)
            shifts = grisk.sensitivity(cases, perturbation=0.2, thresholds=overrides or None)
            cols = [("阈值", 24, "left"), ("基准", 9, "right"), ("-20%", 9, "right"),
                    ("翻转率", 9, "right"), ("+20%", 9, "right"), ("翻转率", 9, "right")]
            print("  " + " ".join(_pad(n, w, a) for n, w, a in cols))
            print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))
            for s in shifts:
                print("  " + " ".join([
                    _pad(s.name, 24),
                    _pad(f"{s.base:g}", 9, "right"),
                    _pad(f"{s.low:g}", 9, "right"),
                    _pad(f"{s.flip_rate_low * 100:.1f}%", 9, "right"),
                    _pad(f"{s.high:g}", 9, "right"),
                    _pad(f"{s.flip_rate_high * 100:.1f}%", 9, "right"),
                ]))
        else:
            print("\n敏感性：没有可用用例（冰坝需 ≥3 期参照；坝型未识别则无从扰动）。")

    cols = [("日期", 12, "left"), ("面积 km²", 10, "right"), ("判据值", 10, "right"),
            ("等级", 9, "left"), ("说明", 40, "left")]
    print()
    print("  " + " ".join(_pad(n, w, a) for n, w, a in cols))
    print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))
    for row in results:
        value = "-" if row.value is None else f"{row.value:.4f}"
        note = row.policy
        print("  " + " ".join([
            _pad(row.date, 12),
            _pad(f"{row.area_km2:.4f}", 10, "right"),
            _pad(value, 10, "right"),
            _pad(row.assessment.level.value, 9),
            _pad(note[:38], 40),
        ]))

    levels = [row.assessment.level for row in results]
    print()
    print(
        f"合计 {len(results)} 期：HIGH {levels.count(grisk.RiskLevel.HIGH)}、"
        f"MEDIUM {levels.count(grisk.RiskLevel.MEDIUM)}、"
        f"LOW {levels.count(grisk.RiskLevel.LOW)}、"
        f"UNKNOWN {levels.count(grisk.RiskLevel.UNKNOWN)}"
    )
    if args.date:
        row = results[0]
        print()
        print("=" * 74)
        print(f"{row.date} 判定明细（{row.policy}）")
        print("=" * 74)
        print(row.assessment.report())

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "risk_assessments.json"
    json_path.write_text(
        json.dumps(
            {
                "source": str(args.input),
                "lake_type": args.lake_type,
                "thresholds": {**grisk.DEFAULT_THRESHOLDS, **overrides},
                "rows": [
                    {
                        "date": row.date,
                        "area_km2": row.area_km2,
                        "level": row.assessment.level.value,
                        "value": row.value,
                        "policy": row.policy,
                        "summary": row.assessment.summary,
                        "reasons": row.assessment.reasons,
                    }
                    for row in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    csv_path = out_dir / "risk_series.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "area_km2", "value", "level", "policy"])
        for row in results:
            writer.writerow([row.date, f"{row.area_km2:.6f}",
                             "" if row.value is None else f"{row.value:.6f}",
                             row.assessment.level.value, row.policy])
    print(f"\n输出      : {json_path}")
    print(f"            {csv_path}")
    return 0


def cmd_accuracy(args: argparse.Namespace) -> int:
    """对整个评估窗口集做点位级召回评价。

    口径（写进报告时必须原样声明，见 docs/精度评价契约.md）：
    * 分母是**被影像覆盖的点位数**，不是 ICIMOD 的点位总数；
    * 抓取阶段没拿到影像的点位单独记入 missing，不计入分母；
    * 提取用的配方与产品一致（同一阈值、同一冰雪掩膜），
      不能为了把精度做高而另调一套参数。
    """
    from .core import accuracy as gacc
    from .core import evalwindows as gew

    windows, missing = gew.load_windows(args.windows)
    if not windows:
        print(f"{args.windows} 里没有可用窗口。先跑 scripts/fetch_eval_windows.py。",
              file=sys.stderr)
        return 1

    print(f"评估窗口  : {len(windows)} 个（另有 {len(missing)} 个点位无可用影像）")
    print(f"提取配方  : NDWI > {args.threshold}，最小面积 {args.min_area_km2} km²，"
          f"{'启用' if not args.no_snow_mask else '关闭'}冰雪掩膜")
    print(f"召回半径  : {args.radius_km} km")
    print()

    results = []
    for win in windows:
        res = gew.extract_window(
            win,
            threshold=args.threshold,
            min_area_km2=args.min_area_km2,
            snow_mask=not args.no_snow_mask,
        )
        results.append(res)
        if not res.ok:
            print(f"  {win.site_id} {win.name[:20]:22s} "
                  f"{win.lat:7.3f},{win.lon:8.3f}  失败 {res.error}")
            continue
        # NDWI 极值标出"这一带到底有没有水"：低于 0.05 说明湖已排空，
        # 提出 0 条是事实，但不能和"湖在、算法漏了"混为一谈
        dried = " [NDWI 极低，疑已排空]" if res.ndwi_max < 0.05 else ""
        print(f"  {win.site_id} {win.name[:20]:22s} "
              f"{win.lat:7.3f},{win.lon:8.3f}  {len(res.records):3d} 个体"
              f"  NDWI max {res.ndwi_max:.3f}{dried}")

    # 点位真值 → 匹配
    truth = [(w.site_id, w.lat, w.lon) for w in windows]
    by_site = {r.window.site_id: r.records for r in results}
    matches = []
    for site_id, lat, lon in truth:
        recs = by_site.get(site_id, [])
        m = gacc.match_points([(site_id, lat, lon)], recs, radius_km=args.radius_km)
        matches.extend(m)

    n_matched = sum(1 for m in matches if m.matched)
    n_failed = sum(1 for r in results if not r.ok)
    recall = n_matched / len(windows) if windows else 0.0
    # "点位附近已无水"的窗口数：低召回率的主要成因，随结果一起落盘
    n_dry = sum(1 for r in results if r.ok and r.ndwi_max < 0.05)
    n_far = sum(1 for m in matches if m.distance_km is not None and m.distance_km > 1.0)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        # n_truth / n_covered 是报告模块算召回率分母用的。原先只写 n_windows，
        # 报告拿不到分母就只能显示「没量到」——那是报告的诚实，但根源在 CLI
        # 没把已有的数写全。这里补齐，让下游不必再猜。
        "n_truth": len(windows) + len(missing),
        "n_covered": len(windows),
        "n_windows": len(windows),
        # 覆盖率 = 被覆盖点位 ÷ 真值总数。报告要它，缺了会显示「没量到」。
        "coverage_rate": round(
            len(windows) / (len(windows) + len(missing)), 4
        ) if (len(windows) + len(missing)) else None,
        "n_missing_imagery": len(missing),
        "n_extract_failed": n_failed,
        "n_matched": n_matched,
        "point_recall": round(recall, 4),
        "radius_km": args.radius_km,
        "threshold": args.threshold,
        "min_area_km2": args.min_area_km2,
        "notes": [
            "point_recall 分母是被影像覆盖的点位数，不含无影像点位",
            "已排空（NDWI 最大值 < 0.05）的点位**计入分母**，算作漏检——"
            "不剔除，避免口径被人质疑为挑数据",
            f"提取参数：NDWI > {args.threshold}，最小面积 {args.min_area_km2} km²。"
            "面积门限用的是评价口径 0.001，不是产品默认的 0.01；"
            "两者不能混报（实测产品口径只提出 2/21，评价口径可提出 14/21）",
            "ICIMOD 为历史溃决事件库，溃决后湖体可能已消失；"
            "本指标测的是当前仍存在水体的湖的检出率，不是 GLOF 识别准确率",
            "未做边界精度评价（无人工标注的面真值）",
            f"实测 {n_far} 个点位提出了水体但距点位 > 1 km（ICIMOD 历史事件坐标"
            "与当前残湖中心不一致）；另有 "
            f"{n_dry} 个窗口 NDWI 最大值 < 0.05，附近确实无水。"
            "这两者是低召回率的主因，不是算法性能",
        ],
        "matches": [
            {
                "site_id": m.site_id,
                "truth_lat": m.truth_lat,
                "truth_lon": m.truth_lon,
                "distance_km": None if m.distance_km is None else round(m.distance_km, 4),
                "matched": m.matched,
                "lake_id": m.lake_id,
                "lake_area_km2": None if m.lake_area_km2 is None else round(m.lake_area_km2, 6),
            }
            for m in matches
        ],
        "missing_imagery": missing,
        "extract_errors": [
            {"site_id": r.window.site_id, "error": r.error}
            for r in results if not r.ok
        ],
    }
    (out_dir / "accuracy.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with (out_dir / "per_site.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["site_id", "name", "distance_km", "matched",
                         "lake_id", "lake_area_km2"])
        name_of = {w.site_id: w.name for w in windows}
        for m in matches:
            writer.writerow([
                m.site_id, name_of.get(m.site_id, ""),
                "" if m.distance_km is None else f"{m.distance_km:.4f}",
                int(m.matched),
                "" if m.lake_id is None else m.lake_id,
                "" if m.lake_area_km2 is None else f"{m.lake_area_km2:.6f}",
            ])

    print()
    print("=" * 66)
    print(f"命中        : {n_matched}/{len(windows)}")
    print(f"点位召回率  : {recall * 100:.1f}%（半径 {args.radius_km} km）")
    if n_failed:
        print(f"提取失败    : {n_failed} 个（已记入 accuracy.json，未当成'无水体'）")
    if missing:
        print(f"无影像点位  : {len(missing)} 个（不计入分母）")
    print(f"结果        : {out_dir / 'accuracy.json'}")
    print(f"              {out_dir / 'per_site.csv'}")
    print("=" * 66)
    print("注意：这是点位召回，不是边界精度；报告里不能写成 IoU/F1。")
    # 低召回率的成因必须和数字一起给，否则会被读成"算法很差"。
    # 实测（26 个窗口）：只有 1 个窗口的 NDWI 最大值 < 0.05（那里确实没水）；
    # 有 16 个窗口提出了水体，但其中 12 个距点位 1.1–3.1 km。
    # 这个"1 km 以上的偏移簇"才是主因：ICIMOD 的坐标是历史事件点，
    # 与当前残湖的几何中心不是一回事，0.15 km 的搜索半径够不着。
    dists = sorted(m.distance_km for m in matches if m.distance_km is not None)
    if recall < 0.5:
        print()
        if dists:
            far = sum(1 for d in dists if d > 1.0)
            print(f"成因说明：{len(dists)} 个点位提出了水体，但 {far} 个距离点位 > 1 km"
                  f"（中位 {dists[len(dists) // 2]:.2f} km，最大 {dists[-1]:.2f} km）。")
            print("  ICIMOD 记录的是历史事件坐标，与当前残湖的几何中心不一致；")
            print("  0.15 km 的搜索半径够不着这个偏移，所以不计为命中。")
        if n_dry:
            print(f"  另有 {n_dry} 个窗口 NDWI 最大值 < 0.05，点位附近确实已无水。")
        print("  结论：低召回率反映真值口径（历史事件点 ≠ 当前湖位），"
              "不是提取算法性能。")
        print("  要评价算法检出能力，需要用有人工标注面真值的数据集。")
    return 0


def cmd_exposure(args: argparse.Namespace) -> int:
    """暴露度评估：算每个湖到最近道路 / 居民点的球面距离。

    数据必须由调用方提供，本命令**不内置任何地理数据**。原因写在
    ``core/exposure.py`` 里：OSM Overpass 2026-11-02 实测不可达
    （overpass-api.de 返回 502，kumi.systems 超时）。编一份 G314 坐标
    比缺数据危险得多——缺数据下游会走 UNKNOWN，编的坐标会被当真值用。

    ``--settlements`` 不给时，居民点两个字段是 None（不是 False）：
    False 表示"量到了且不在范围内"，None 才是"没数据"。
    """
    from .core import exposure as gexp
    from .core.water import extract_lakes

    if not Path(args.roads).exists():
        print(f"道路文件不存在：{args.roads}", file=sys.stderr)
        print("  暴露度需要外部矢量数据（GeoJSON）。本项目不内置地理数据，"
              "原因见 glof/core/exposure.py 模块文档。", file=sys.stderr)
        return 1
    if not args.lakes and not args.input:
        print("要给湖泊来源：--lakes <已有GeoJSON> 或 --input <影像>。", file=sys.stderr)
        return 1
    if args.lakes and not Path(args.lakes).exists():
        print(f"湖泊文件不存在：{args.lakes}", file=sys.stderr)
        return 1
    if args.settlements and not Path(args.settlements).exists():
        print(f"居民点文件不存在：{args.settlements}", file=sys.stderr)
        return 1

    # 先拿到湖泊：要么从已有 GeoJSON 读，要么现场提取
    if args.lakes:
        records = gio.load_lakes_geojson(args.lakes)
    elif args.input:
        scene = gio.load_scene(args.input, BAND_PRESETS[args.preset])
        n = ndwi(scene["green"], scene["nir"])
        snow = masks.snow_mask(ndsi(scene["green"], scene["swir1"]), scene["nir"]) \
            if scene.has("swir1") else None
        records = extract_lakes(
            n, scene.transform, scene.crs,
            threshold=args.threshold, min_area_km2=args.min_area_km2, exclude=snow,
        )

    if not records:
        print("没有可评估的湖泊（提取到 0 条）。这不是「没有风险」，是没有水体。",
              file=sys.stderr)
        return 1

    roads = gexp.load_features(args.roads)
    settlements = gexp.load_features(args.settlements) if args.settlements else None
    out = gexp.assess_exposure(
        records, roads_path=args.roads,
        settlements_path=args.settlements, buffer_km=args.buffer_km,
    )

    print(f"道路要素  : {len(roads)}")
    print(f"居民点    : {'未提供（相关字段为 None，不是 False）' if settlements is None else len(settlements)}")
    print(f"缓冲距离  : {args.buffer_km} km")
    print()
    print(f"  {'编号':>4}  {'道路 km':>9}  {'在缓冲内':>8}  {'居民点 km':>10}  {'在缓冲内':>8}")
    for rec in out:
        road = "没量到" if rec.road_distance_km is None else f"{rec.road_distance_km:.3f}"
        sett = "没量到" if rec.settlement_distance_km is None else f"{rec.settlement_distance_km:.3f}"
        rin = "未判定" if rec.in_road_buffer is None else ("是" if rec.in_road_buffer else "否")
        sin = "未判定" if rec.in_settlement_buffer is None else ("是" if rec.in_settlement_buffer else "否")
        print(f"  {rec.lake_id:>4}  {road:>9}  {rin:>8}  {sett:>10}  {sin:>8}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "lake_id": r.lake_id,
            "road_distance_km": None if r.road_distance_km is None else round(r.road_distance_km, 4),
            "settlement_distance_km": None if r.settlement_distance_km is None else round(r.settlement_distance_km, 4),
            "in_road_buffer": r.in_road_buffer,
            "in_settlement_buffer": r.in_settlement_buffer,
        }
        for r in out
    ]
    (out_dir / "exposure.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"结果      : {out_dir / 'exposure.json'}")
    print("注意：这是到道路/居民点的直线距离，不是洪水淹没范围。")
    print("      真正的暴露度需要水动力模型，本指标只是可复算的筛查代理。")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """把已有产物汇总成一份报告（Markdown + 自包含 HTML）。

    只读已落盘的结果，不重新计算——报告里的每个数都必须能追溯到
    某个 JSON/CSV。缺文件就跳过该章节并在报告里写明，不编造。
    """
    from .core import report as grep_mod

    sections = []
    meta = {"数据来源": [], "口径": []}
    # build_lake_report 返回的是**完整文档**（自带 # 标题、元信息、页脚），
    # 而 build_accuracy_report / build_risk_report 返回的是**章节**
    # （从 ## 开始，设计上塞进 ReportSection.body）。两者语义不同，不能
    # 一视同仁地塞进 section，否则标题会套两层。湖泊清单走独立出口。
    lake_doc: str | None = None

    # 湖泊清单（可选：从 GeoJSON 或直接跑提取）
    if args.lakes:
        records = gio.load_lakes_geojson(args.lakes)
        if records:
            lake_doc = grep_mod.build_lake_report(
                records, title=args.title,
                meta={"数据来源": f"湖泊：{args.lakes}"},
            )
            meta["数据来源"].append(f"湖泊：{args.lakes}")

    # 精度评价
    acc_path = Path(args.accuracy) if args.accuracy else None
    if acc_path and acc_path.exists():
        payload = json.loads(acc_path.read_text(encoding="utf-8"))
        # body 自带「## 精度评价（点位级，弱真值）」，section 不再另给 heading，
        # 否则会渲染成「## 精度评价」下面又套一个「## 精度评价（…）」。
        sections.append(grep_mod.ReportSection(
            heading="",
            body=grep_mod.build_accuracy_report(payload),
        ))
        meta["数据来源"].append(f"精度：{acc_path}")

    # 风险分级
    if args.risk:
        risk_path = Path(args.risk)
        if risk_path.exists():
            payload = json.loads(risk_path.read_text(encoding="utf-8"))
            rows = payload.get("periods") or payload.get("rows") or []
            if rows:
                sections.append(grep_mod.ReportSection(
                    heading="",
                    body=grep_mod.build_risk_report(rows, title="风险分级"),
                ))
                meta["数据来源"].append(f"风险：{risk_path}")

    if lake_doc is None and not sections:
        print("没有任何可汇总的产物。至少给一个：--lakes / --accuracy / --risk",
              file=sys.stderr)
        return 1

    if lake_doc is not None:
        # 湖泊清单自带完整文档结构；其余章节拼在后面（它们本来就是章节形态）。
        #
        # 拼接方式是「在页脚前插入」，不是「拆字符串再拼回去」：
        # build_markdown 的页脚是固定的三行（--- / 数据来源 / 生成时间），
        # 拿 rsplit("\n---\n") 切会在正文里出现别的分隔线时切错位置。
        text = lake_doc
        if sections:
            extra = grep_mod.build_markdown(
                title=args.title, sections=sections, meta={},
            )
            # 取 extra 里第一个 "## " 到页脚 "---" 之间的正文
            start = extra.find("\n## ")
            end = extra.rfind("\n---\n")
            body = extra[start:end].strip() if start >= 0 and end > start else ""
            if body:
                # 插到 lake_doc 自己的页脚之前
                footer_at = text.rfind("\n---\n")
                if footer_at > 0:
                    text = text[:footer_at].rstrip() + "\n\n" + body + "\n" + text[footer_at:]
                else:
                    text = text.rstrip() + "\n\n" + body + "\n"
    else:
        text = grep_mod.build_markdown(
            title=args.title, sections=sections,
            meta={k: v for k, v in meta.items() if v},
        )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = grep_mod.write_markdown(out_dir / "report.md", text)
    html_path = out_dir / "report.html"
    html_path.write_text(grep_mod.to_html(text, title=args.title), encoding="utf-8")

    # section 的 heading 可能为空（body 自带标题时不重复加），此时打印空括号很怪
    names = [s.heading for s in sections if s.heading]
    extra = "湖泊清单" if lake_doc is not None else ""
    if extra:
        names.insert(0, extra)
    print(f"章节      : {len(names)} 个（{', '.join(names) if names else '无标题章节'}）")
    print(f"Markdown  : {md_path}")
    print(f"HTML      : {html_path}（自包含，无外部依赖，可直接归档）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="glof", description="帕米尔冰湖识别与风险筛查")
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="查看栅格元数据")
    p_info.add_argument("--input", required=True, help="输入 GeoTIFF 路径")
    p_info.set_defaults(func=cmd_info)

    p_fetch = sub.add_parser("fetch", help="检索 Sentinel-2 并可落成本地窗口")
    p_fetch.add_argument(
        "--bbox",
        nargs=4,
        required=True,
        metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
        help="研究区范围（WGS84 经纬度）",
    )
    p_fetch.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    p_fetch.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p_fetch.add_argument("--max-cloud", type=float, default=10.0, help="整景云量上限（%%）")
    p_fetch.add_argument("--limit", type=int, default=10, help="最多检索多少景")
    p_fetch.add_argument("--out", default="data/raw", help="保存目录")
    p_fetch.add_argument("--save", type=int, nargs="?", const=1, default=0,
                         help="保存前 N 景到本地（不带数字默认 1）")
    p_fetch.add_argument("--bands", default="s2_l2a_6band", choices=sorted(BAND_PRESETS))
    p_fetch.add_argument(
        "--endpoint", default=gstac.PC_ENDPOINT, help="STAC 端点（默认 Planetary Computer）"
    )
    p_fetch.add_argument("--platform", default="pc", choices=["pc", "cdse"], help="资产命名规范")
    p_fetch.add_argument("--token", default=None, help="CDSE 的 Bearer token（PC 不需要）")
    p_fetch.set_defaults(func=cmd_fetch)

    p_latest = sub.add_parser(
        "latest",
        help="打印研究区的最新可用观测（影像有拍摄日期，会显示距今几天）",
    )
    p_latest.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
        help="研究区范围（WGS84 经纬度）",
    )
    p_latest.add_argument("--days", type=int, default=20,
                          help="往前检索多少天（默认 20；重访 5 天，留出连续多云的余量）")
    p_latest.add_argument("--max-cloud", type=float, default=20.0,
                          help="整景云量上限（%%），默认 20")
    p_latest.add_argument("--min-area-km2", type=float, default=0.01,
                          help="最小面积过滤（km²），默认 0.01（与产品口径一致）")
    p_latest.add_argument("--json", action="store_true",
                          help="输出机器可读 JSON 到 stdout；进度与提示改走 stderr")
    p_latest.add_argument("--save", action="store_true",
                          help="把提取结果落成 GeoJSON + CSV（与 extract 同一套格式）")
    p_latest.add_argument("--out", default="output/latest",
                          help="--save 的输出目录")
    p_latest.set_defaults(func=cmd_latest)

    p_inv = sub.add_parser("inventory", help="从 ICIMOD HMA GLOF 数据库筛研究区溃决湖")
    p_inv.add_argument("--db", default="data/raw/GLOF database of High Mountain Asia.zip",
                       help="数据库 zip / 目录 / csv")
    p_inv.add_argument("--bbox", nargs=4, type=float, required=True,
                       metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    p_inv.add_argument("--country", default=None, help="只保留某个国家，如 China")
    p_inv.add_argument("--cluster-m", type=float, default=1000.0,
                       help="同一湖的事件聚类半径（米）")
    p_inv.add_argument("--out", default="output/inventory")
    p_inv.set_defaults(func=cmd_inventory)

    p_ext = sub.add_parser("extract", help="提取湖泊并统计面积")
    p_ext.add_argument("--input", required=True, help="输入 GeoTIFF 路径")
    p_ext.add_argument("--out", default="output/run", help="输出目录")
    p_ext.add_argument(
        "--bands",
        default="s2_l2a_6band",
        choices=sorted(BAND_PRESETS),
        help="波段预设（决定哪几个波段当 Green/NIR/SWIR）",
    )
    p_ext.add_argument("--threshold", default="0.0", help='NDWI 阈值，数字或 "otsu"')
    p_ext.add_argument("--min-area-km2", type=float, default=0.01, help="最小面积过滤（km²）")
    p_ext.add_argument("--ndsi-threshold", type=float, default=0.4, help="NDSI 冰雪阈值")
    p_ext.add_argument("--no-snow-mask", action="store_true", help="关闭冰雪掩膜（消融对比用）")
    p_ext.add_argument("--close-px", type=int, default=0, help="形态学闭运算迭代次数")
    p_ext.add_argument("--no-fill-holes", action="store_true", help="不填湖面内部空洞")
    p_ext.add_argument("--scale", default="auto", help='反射率缩放，数字或 "auto"')
    p_ext.add_argument("--downsample", type=int, default=1, help="降采样倍数（仅预览，面积会偏小）")
    p_ext.add_argument("--save-mask", action="store_true", help="另存水体掩膜 GeoTIFF")
    p_ext.add_argument("--top", type=int, default=15, help="表格显示前 N 条")
    p_ext.set_defaults(func=cmd_extract)

    p_risk = sub.add_parser("risk", help="对面积时序逐期做溃决风险分级（留出法，无权重）")
    p_risk.add_argument("--input", required=True,
                        help="时序 CSV（兼容 kyagar_area_series / kyagar_dense 两种列格式）")
    p_risk.add_argument("--lake-type", required=True,
                        help="坝型：ice（冰坝）或 moraine（冰碛坝）")
    p_risk.add_argument("--date", default=None,
                        help="只评估这一期（YYYY-MM-DD，必须存在于序列）；缺省评估全部期")
    p_risk.add_argument("--baseline-date", default=None,
                        help="冰碛坝基准期（YYYY-MM-DD）；缺省用序列最早一期")
    p_risk.add_argument("--slope-deg", type=float, default=None,
                        help="冰碛坝：坝体下游坡度（度）；不传则该判据记缺数据")
    p_risk.add_argument("--dem", default=None,
                        help="DEM 路径或 Copernicus 瓦片 URL；给了就从 DEM 量坝体下游坡度")
    p_risk.add_argument("--dam-position", action="store_true",
                        help="从湖边界最低点自动估坝体位置（需 --dem 与 --lakes）。"
                             "比拿湖心当坝体准——湖心是平底，量不出坡向")
    p_risk.add_argument("--lakes", default=None,
                        help="湖泊 GeoJSON（配合 --dam-position，坝体从湖边界几何估）")
    p_risk.add_argument("--dem-lat", type=float, default=None,
                        help="坝体纬度（配合 --dem；通常取湖心）")
    p_risk.add_argument("--dem-lon", type=float, default=None,
                        help="坝体经度（配合 --dem；通常取湖心）")
    p_risk.add_argument("--glacier-contact", default=None, choices=["yes", "no"],
                        help="冰碛坝：是否与母冰川接触；不传则该判据记缺数据")
    p_risk.add_argument("--min-valid-frac", type=float, default=0.0,
                        help="过滤有效覆盖低于该值的期（窄条）；CSV 无该列时不过滤")
    p_risk.add_argument("--threshold", action="append", default=None, metavar="NAME=VALUE",
                        help="覆盖默认阈值，可重复：plateau_ratio=0.9 等")
    p_risk.add_argument("--sensitivity", action="store_true",
                        help="追加阈值 ±20%% 敏感性分析（等级翻转率）")
    p_risk.add_argument("--out", default="output/risk", help="输出目录（JSON + CSV）")
    p_risk.set_defaults(func=cmd_risk)

    p_acc = sub.add_parser(
        "accuracy", help="对评估窗口集做点位级召回评价（ICIMOD 弱真值）")
    p_acc.add_argument("--windows", default="data/eval/windows/windows.json",
                       help="抓取阶段产出的清单")
    p_acc.add_argument("--radius-km", type=float, default=0.15,
                       help="点位命中的搜索半径（km）。默认 0.15 约等于 15 个像元，"
                            "足够吸收 ICIMOD 坐标误差，又不至于把邻湖算成命中")
    p_acc.add_argument("--threshold", type=float, default=0.15,
                       help="NDWI 阈值，与产品默认一致")
    p_acc.add_argument("--min-area-km2", type=float, default=0.001,
                       help="最小面积。默认 0.001 km² 用于评价算法检出能力；"
                            "产品默认是 0.01，两个口径不能混报")
    p_acc.add_argument("--no-snow-mask", action="store_true",
                       help="关闭冰雪掩膜（做消融对比用，不要用于正式报告）")
    p_acc.add_argument("--out", default="output/eval", help="输出目录")
    p_acc.set_defaults(func=cmd_accuracy)

    p_exp = sub.add_parser(
        "exposure",
        help="暴露度：算湖到最近道路 / 居民点的球面距离（需自备矢量数据）",
    )
    p_exp.add_argument("--input", default=None,
                       help="输入 GeoTIFF（现场提取湖泊时用）")
    p_exp.add_argument("--lakes", default=None,
                       help="已有的湖泊 GeoJSON（优先于 --input，省掉重读影像）")
    p_exp.add_argument("--preset", default="s2_l2a_6band", choices=sorted(BAND_PRESETS),
                       help="波段预设（现场提取时用）")
    p_exp.add_argument("--threshold", type=float, default=0.15,
                       help="NDWI 阈值（现场提取时用），默认 0.15")
    p_exp.add_argument("--min-area-km2", type=float, default=0.01,
                       help="最小面积 km²，默认 0.01（产品口径）")
    p_exp.add_argument("--roads", required=True,
                       help="道路矢量（GeoJSON）。本项目不内置地理数据，"
                            "原因见 glof/core/exposure.py 模块文档")
    p_exp.add_argument("--settlements", default=None,
                       help="居民点矢量（GeoJSON）。不给时相关字段为 None（未判定），不是 False")
    p_exp.add_argument("--buffer-km", type=float, required=True,
                       help="影响范围半径（km）。**必须显式给**——阈值不藏默认值")
    p_exp.add_argument("--out", default="output/exposure", help="输出目录")
    p_exp.set_defaults(func=cmd_exposure)

    p_rep = sub.add_parser(
        "report",
        help="把已有产物汇总成报告（Markdown + 自包含 HTML，不新增依赖）",
    )
    p_rep.add_argument("--lakes", default=None, help="湖泊 GeoJSON（extract 产出）")
    p_rep.add_argument("--accuracy", default=None, help="accuracy.json")
    p_rep.add_argument("--risk", default=None, help="风险分级 JSON")
    p_rep.add_argument("--title", default="帕米尔冰湖档案 · 评估报告")
    p_rep.add_argument("--out", default="output/report", help="输出目录")
    p_rep.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
