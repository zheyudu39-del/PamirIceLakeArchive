"""测试区选址：从 OSM 水体出发，逐个用真实影像和 DEM 评估，出一张候选对比图。

为什么要这一步：阈值、精度、召回率这些指标，只有落在**确定的测试湖**上才有意义。
凭经纬度框一片山，跑出来的数字既不能验证也不能写进报告。

数据来源与角色：
    OSM 水体    独立的第三方参考（不是我们自己提的，避免循环论证）
    Sentinel-2  用于确认"这个位置现在确实是水体"，并量 NDWI 对比度
    Copernicus DEM  用于判断海拔，冰湖基本都在 4000 m 以上

输出：
    candidates.csv      每个候选的面积、海拔、NDWI、检出面积、紧凑度
    contact_sheet.png   缩略图对比，肉眼一眼能挑

用法：
    ./.venv/Scripts/python.exe scripts/candidate_sites.py \
        --bbox 74.95 36.90 76.50 38.70 --min-area-km2 0.02 --max-sites 9
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pyproj import CRS, Transformer  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402
from shapely.ops import transform as shp_transform  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import masks  # noqa: E402
from glof.core import stac as gstac  # noqa: E402
from glof.core.indices import S2_L2A_6BAND, ndsi, ndwi  # noqa: E402
from glof.core.water import build_water_mask  # noqa: E402

UA = {"User-Agent": "glof-research/0.1 (challenge-cup)"}
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACKS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
# 高程源暂不可用，见 main() 里的说明


@dataclass
class Candidate:
    osm_id: str
    name: str
    lon: float
    lat: float
    area_km2: float
    compactness: float
    polygon: object = None  # shapely Polygon，WGS84
    scene: str = ""
    scene_date: str = ""
    ndwi_mean: float = float("nan")
    detected_km2: float = float("nan")
    recall: float = float("nan")
    thumb: Image.Image | None = None


def overpass_water(bbox) -> list[dict]:
    west, south, east, north = bbox
    query = f"""[out:json][timeout:90];
(
  way["natural"="water"]({south},{west},{north},{east});
  way["water"="lake"]({south},{west},{north},{east});
);
out geom;"""
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for endpoint in OVERPASS_FALLBACKS:
        try:
            req = urllib.request.Request(endpoint, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=100) as resp:
                return json.load(resp).get("elements", [])
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  {endpoint} 失败：{type(exc).__name__} {exc}，换下一个")
    raise RuntimeError(f"所有 Overpass 端点都失败：{last}")


def to_candidates(elements: list[dict], min_area_km2: float) -> list[Candidate]:
    out: list[Candidate] = []
    for el in elements:
        geom = el.get("geometry") or []
        if len(geom) < 4:
            continue
        ring = [(p["lon"], p["lat"]) for p in geom if "lon" in p]
        if len(ring) < 4:
            continue
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area == 0:
            continue

        c = poly.centroid
        utm = CRS.from_epsg((32600 if c.y >= 0 else 32700) + int((c.x + 180) // 6) + 1)
        to_utm = Transformer.from_crs("EPSG:4326", utm, always_xy=True)
        poly_m = shp_transform(to_utm.transform, poly)

        area_km2 = poly_m.area / 1e6
        if area_km2 < min_area_km2:
            continue
        # 紧凑度 4πA/P²：1 是圆，越小越狭长。河流通常 <0.1，湖 >0.3
        compact = 4 * np.pi * poly_m.area / max(poly_m.length**2, 1e-9)

        out.append(
            Candidate(
                osm_id=f"{el['type']}/{el['id']}",
                name=(el.get("tags") or {}).get("name", "-"),
                lon=float(c.x),
                lat=float(c.y),
                area_km2=float(area_km2),
                compactness=float(compact),
                polygon=poly,
            )
        )
    out.sort(key=lambda x: x.area_km2, reverse=True)
    return out


def evaluate_site(c: Candidate, pad_deg: float) -> Candidate:
    """抓一小块 S2 影像，在 **OSM 多边形内部**统计检出情况。

    为什么必须在多边形内部统计：大湖的质心附近全是被水，任何小窗口都会得到"100% 检出"，
    这个数字毫无信息量。真正的指标是"OSM 多边形里有多少像元被判成水"，也就是召回率。
    """
    from rasterio.features import rasterize

    bbox = [c.lon - pad_deg, c.lat - pad_deg, c.lon + pad_deg, c.lat + pad_deg]
    refs = gstac.search_sentinel2(bbox, "2025-06-01", "2025-09-30", max_cloud=10, limit=1)
    if not refs:
        return c
    ref = refs[0]
    c.scene, c.scene_date = ref.id, ref.date
    try:
        sources, headers = gstac.sign_scene(ref)
        sc = gio.load_scene(sources, S2_L2A_6BAND, bounds=bbox, http_headers=headers or None)
    except Exception:
        return c

    n = ndwi(sc["green"], sc["nir"])
    s = ndsi(sc["green"], sc["swir1"])
    water = build_water_mask(n, 0.15, exclude=masks.snow_mask(s, sc["nir"], 0.4))

    # rasterize 要求几何与 transform 同一坐标系，而 OSM 给的是 WGS84、影像是 UTM
    to_scene = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    poly_scene = shp_transform(to_scene.transform, c.polygon)

    ref_mask = rasterize(
        [(poly_scene, 1)],
        out_shape=water.shape,
        transform=sc.transform,
        fill=0,
        dtype="uint8",
    ).astype(bool)

    inside = ref_mask & np.isfinite(n)
    n_px = int(inside.sum())
    if n_px:
        c.ndwi_mean = float(np.nanmean(n[inside]))
        c.detected_km2 = float((water & inside).sum() * sc.pixel_area_m2 / 1e6)
        c.recall = float((water & inside).sum() / n_px)
    else:
        # 多边形落在影像有效区之外（云/边缘），如实标注而不是给个 0
        c.ndwi_mean = float("nan")
        c.detected_km2 = float("nan")
        c.recall = float("nan")

    rgb = np.dstack([_stretch(sc["red"]), _stretch(sc["green"]), _stretch(sc["blue"])])
    water_vis = rgb.copy()
    water_vis[water] = [255, 40, 40]
    water_vis[ref_mask & ~water] = [40, 200, 255]  # 青色 = OSM 有、我们没检出
    c.thumb = Image.fromarray(np.hstack([rgb, water_vis]))
    return c


def _stretch(band: np.ndarray, low=2, high=98) -> np.ndarray:
    v = band[np.isfinite(band)]
    if v.size == 0:
        return np.zeros(band.shape, np.uint8)
    lo, hi = np.percentile(v, [low, high])
    if hi <= lo:
        hi = lo + 1e-6
    return (np.clip((np.nan_to_num(band, nan=lo) - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def build_sheet(items: list[Candidate], out: Path, cols: int = 3, size: int = 240) -> Path:
    items = [c for c in items if c.thumb is not None]
    if not items:
        raise RuntimeError("没有可用的缩略图")
    rows = (len(items) + cols - 1) // cols
    cell_h = size + 26
    canvas = Image.new("RGB", (cols * size, rows * cell_h), (22, 22, 24))
    draw = ImageDraw.Draw(canvas)
    for i, c in enumerate(items):
        r, col = divmod(i, cols)
        thumb = c.thumb.resize((size, size), Image.BILINEAR)
        canvas.paste(thumb, (col * size, r * cell_h))
        label = f"{c.osm_id.split('/')[-1]}  OSM {c.area_km2:.3f}km2  {c.name[:10]}"
        draw.text((col * size + 4, r * cell_h + size + 3), label, fill=(225, 225, 225))
        draw.text((col * size + 4, r * cell_h + size + 13),
                  f"NDWI {c.ndwi_mean:+.2f}  det {c.detected_km2:.3f}  recall {c.recall*100:.0f}%",
                  fill=(150, 150, 150))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("W", "S", "E", "N"))
    ap.add_argument("--min-area-km2", type=float, default=0.02)
    ap.add_argument("--max-sites", type=int, default=9)
    ap.add_argument("--min-compactness", type=float, default=0.15,
                    help="紧凑度下限，用于排除河道")
    ap.add_argument("--pad-deg", type=float, default=0.02, help="每个候选的裁剪半宽（度）")
    ap.add_argument("--out", default="output/sites")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("步骤 1 / 从 OSM 取候选水体")
    print("=" * 70)
    elements = overpass_water(args.bbox)
    cands = to_candidates(elements, args.min_area_km2)
    print(f"  OSM 要素 {len(elements)} 个，面积 ≥ {args.min_area_km2} km² 的 {len(cands)} 个")

    cands = cands[: args.max_sites * 2]

    print()
    print("=" * 70)
    print("步骤 2 / 按紧凑度筛选（排除河道）")
    print("=" * 70)
    print("  说明：海拔筛选跳过——公开高程接口在本机不可达，PC 的 DEM 令牌对该存储账户返回 403。")
    print("  改用紧凑度 4πA/P²：圆形湖泊接近 1，狭长河道 <0.1。冰湖通常 >0.2。")
    shaped = [c for c in cands if c.compactness >= args.min_compactness]
    print(f"  紧凑度 ≥ {args.min_compactness} 的 {len(shaped)} 个")
    for c in shaped[:12]:
        print(f"    {c.osm_id:<14} {c.area_km2:7.3f} km²  紧凑度 {c.compactness:.2f}  {c.name}")

    picked = shaped[: args.max_sites]

    print()
    print("=" * 70)
    print("步骤 3 / 逐个用 Sentinel-2 确认")
    print("=" * 70)
    for i, c in enumerate(picked, 1):
        evaluate_site(c, args.pad_deg)
        print(f"  [{i}/{len(picked)}] {c.osm_id:<14} {c.area_km2:6.3f} km²  "
              f"NDWI {c.ndwi_mean:+.2f}  检出 {c.detected_km2:6.3f} km²  "
              f"召回 {c.recall*100:5.1f}%  {c.scene_date}")

    csv_path = out_dir / "candidates.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["osm_id", "name", "lon", "lat", "osm_area_km2", "compactness",
                    "ndwi_mean", "detected_km2", "recall", "scene", "scene_date"])
        for c in picked:
            w.writerow([c.osm_id, c.name, f"{c.lon:.6f}", f"{c.lat:.6f}",
                        f"{c.area_km2:.4f}", f"{c.compactness:.3f}",
                        f"{c.ndwi_mean:.4f}", f"{c.detected_km2:.4f}",
                        f"{c.recall:.4f}", c.scene, c.scene_date])
    print(f"\n  表格: {csv_path}")

    sheet = build_sheet(picked, out_dir / "contact_sheet.png")
    print(f"  对比图: {sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
