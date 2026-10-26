"""在已知湖上对比三种判水规则，看哪一种真的能召回浑浊冰湖。

这一步是整条算法链的"验收"。前面所有参数都是在合成影像或空山框里调的，
只有在**已知真实湖泊**上比出来的数字才算数。

四个测试湖来自 OSM（`output/sites/candidates.csv`），
克亚吉尔来自 ICIMOD HMA GLOF 数据库（34 次溃决记录）。

对比的三种规则：
    A. NDWI > 0.15                          之前的固定阈值
    B. NDWI > 滑窗 Otsu 阈值图              用户要求的本地自适应
    C. NDWI > 0.02 且 NIR < 0.20            光谱双条件（本次假设）

用法：
    ./.venv/Scripts/python.exe scripts/validate_sites.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.features import rasterize  # noqa: E402
from shapely.geometry import Point  # noqa: E402
from shapely.ops import transform as shp_transform  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import masks  # noqa: E402
from glof.core import stac as gstac  # noqa: E402
from glof.core.indices import S2_L2A_6BAND, ndsi, ndwi  # noqa: E402
from glof.core.water import build_water_mask, otsu_threshold_surface  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

RULES = [
    ("A. NDWI>0.15", "fixed"),
    ("B. 滑窗Otsu", "adaptive"),
    ("C. NDWI>0.02+NIR<0.20", "spectral"),
]

KYAGAR = ("Kyagar", 35.677371, 77.190788, 1500.0)  # 名字, 纬度, 经度, 缓冲半径 m


def load_sites() -> list[dict]:
    path = ROOT / "output" / "sites" / "candidates.csv"
    sites = []
    if path.exists():
        for row in csv.DictReader(path.open(encoding="utf-8-sig")):
            sites.append(
                {
                    "name": row["name"] if row["name"] != "-" else f"未名 {row['osm_id']}",
                    "lon": float(row["lon"]),
                    "lat": float(row["lat"]),
                    "ref": "osm",
                }
            )
    sites.append({"name": KYAGAR[0], "lon": KYAGAR[2], "lat": KYAGAR[1], "ref": "point"})
    return sites


def fetch_scene(lon: float, lat: float, pad: float = 0.025):
    bbox = [lon - pad, lat - pad, lon + pad, lat + pad]
    refs = gstac.search_sentinel2(bbox, "2025-06-01", "2025-09-30", max_cloud=10, limit=1)
    if not refs:
        return None, None, None
    ref = refs[0]
    sources, headers = gstac.sign_scene(ref)
    scene = gio.load_scene(sources, S2_L2A_6BAND, bounds=bbox, http_headers=headers or None)
    return scene, ref, bbox


def reference_mask(scene, lon: float, lat: float, ref_kind: str, radius_m: float):
    """参考区：OSM 用不到多边形（CSV 没存几何），退化为圆形缓冲。"""
    r_px = int(round(radius_m / max(scene.pixel_size_m, 1)))
    h, w = scene.shape
    cy, cx = h // 2, w // 2
    yy, xx = np.ogrid[:h, :w]
    return ((yy - cy) ** 2 + (xx - cx) ** 2) <= r_px**2


def main() -> int:
    sites = load_sites()
    print("=" * 78)
    print(f"在 {len(sites)} 个已知湖上对比三种判水规则（2025 消融期影像）")
    print("=" * 78)

    results: dict[str, list[float]] = {label: [] for label, _ in RULES}
    rows = []

    for i, site in enumerate(sites, 1):
        try:
            scene, ref, _ = fetch_scene(site["lon"], site["lat"])
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(sites)}] {site['name'][:12]:<12} 取影像失败 {type(exc).__name__}")
            continue
        if scene is None:
            print(f"  [{i}/{len(sites)}] {site['name'][:12]:<12} 无可用影像")
            continue

        n = ndwi(scene["green"], scene["nir"])
        s = ndsi(scene["green"], scene["swir1"])
        snow = masks.snow_mask(s, scene["nir"], 0.4)
        nir = scene["nir"]

        # 参考区：Kyagar 用 1.5 km 圆，其余用 400 m 圆（OSM 湖都很小）
        radius = KYAGAR[3] if site["ref"] == "point" else 400.0
        ref_mask = reference_mask(scene, site["lon"], site["lat"], site["ref"], radius)
        ref_mask &= np.isfinite(n)

        row = {"name": site["name"], "date": ref.date, "ndwi": float(np.nanmedian(n[ref_mask]))}
        for label, kind in RULES:
            if kind == "fixed":
                water = build_water_mask(n, 0.15, exclude=snow)
            elif kind == "adaptive":
                surf = otsu_threshold_surface(n, window_px=128, min_valid=800)
                water = build_water_mask(n, surf, exclude=snow)
            else:
                water = build_water_mask(n, 0.02, exclude=snow, nir=nir, nir_max=0.20)
            frac = float((water & ref_mask).sum() / max(ref_mask.sum(), 1))
            total_km2 = float(water.sum() * scene.pixel_area_m2 / 1e6)
            outside_km2 = float((water & ~ref_mask).sum() * scene.pixel_area_m2 / 1e6)
            row[label] = frac
            row[label + "_total"] = total_km2
            row[label + "_outside"] = outside_km2
            results[label].append(frac)

        rows.append(row)
        print(
            f"  [{i}/{len(sites)}] {site['name'][:12]:<12} NDWI {row['ndwi']:+.3f}  "
            + "  ".join(f"{lab.split('.')[0]}={row[lab] * 100:5.1f}%" for lab, _ in RULES)
        )
        print(
            "  " + " " * 26
            + "  ".join(
                f"{lab.split('.')[0]}:{row[lab + '_total']:6.2f}km²(外{row[lab + '_outside']:5.2f})"
                for lab, _ in RULES
            )
        )

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    cols = [("规则", 24, "left"), ("平均召回", 10, "right"), ("最低", 9, "right"),
            ("≥50% 的湖", 11, "right"), ("场景总水体", 12, "right"), ("参考区外", 11, "right")]
    print("  " + " ".join(_pad(n, w, a) for n, w, a in cols))
    print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))
    for label, _ in RULES:
        vals = np.array(results[label])
        if vals.size == 0:
            continue
        tot = np.array([r[label + "_total"] for r in rows])
        out = np.array([r[label + "_outside"] for r in rows])
        print(
            "  "
            + " ".join(
                [
                    _pad(label, 24),
                    _pad(f"{vals.mean() * 100:.1f}%", 10, "right"),
                    _pad(f"{vals.min() * 100:.1f}%", 9, "right"),
                    _pad(f"{int((vals >= 0.5).sum())}/{vals.size}", 11, "right"),
                    _pad(f"{tot.mean():.2f} km²", 12, "right"),
                    _pad(f"{out.mean():.2f} km²", 11, "right"),
                ]
            )
        )

    print()
    print("「参考区外」是场景里参考圆之外的水体面积，用来判断这条规则是不是靠")
    print("到处判成水刷出来的召回——如果它比参考区内还大，召回就不可信。")

    csv_path = ROOT / "output" / "sites" / "rule_comparison.csv"
    if rows:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n明细: {csv_path}")
    return 0

    print()
    print("注：参考区是圆形缓冲，不是湖泊真实边界，所以数值只能横向比三种规则，")
    print("    不能当作绝对召回率写进报告。绝对精度需要人工解译的湖泊边界。")
    return 0


def _pad(text: str, width: int, align: str = "left") -> str:
    disp = sum(2 if ord(c) > 127 else 1 for c in text)
    gap = max(0, width - disp)
    return (" " * gap + text) if align == "right" else (text + " " * gap)


if __name__ == "__main__":
    raise SystemExit(main())
