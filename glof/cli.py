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

    try:
        results = grs.assess_series(
            points,
            args.lake_type,
            date=args.date,
            baseline_date=args.baseline_date,
            downstream_slope_deg=args.slope_deg,
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
            downstream_slope_deg=args.slope_deg,
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
    p_risk.add_argument("--glacier-contact", default=None, choices=["yes", "no"],
                        help="冰碛坝：是否与母冰川接触；不传则该判据记缺数据")
    p_risk.add_argument("--min-valid-frac", type=float, default=0.0,
                        help="过滤有效覆盖低于该值的期（窄条）；CSV 无该列时不过滤")
    p_risk.add_argument("--threshold", action="append", default=None, metavar="NAME=VALUE",
                        help="覆盖默认阈值，可重复：plateau_ratio=0.9 等")
    p_risk.add_argument("--sensitivity", action="store_true",
                        help="追加阈值 ±20% 敏感性分析（等级翻转率）")
    p_risk.add_argument("--out", default="output/risk", help="输出目录（JSON + CSV）")
    p_risk.set_defaults(func=cmd_risk)

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
