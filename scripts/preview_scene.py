"""把一景影像渲染成预览图：真彩色 + NDWI + 提取结果。

用途只有一个——肉眼判断提取结果对不对。数字（面积、NDWI 均值）会骗人，
图上是不是湖，一眼就知道。

用法：
    ./.venv/Scripts/python.exe scripts/preview_scene.py \
        --input data/raw/xxx.tif --out output/real1/preview.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import masks  # noqa: E402
from glof.core.indices import S2_L2A_6BAND, ndsi, ndwi  # noqa: E402
from glof.core.water import build_water_mask  # noqa: E402


def stretch(band: np.ndarray, low: float = 2.0, high: float = 98.0) -> np.ndarray:
    """百分位拉伸到 0-255。固定 min/max 会被一两个亮像元毁掉整张图。"""
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros(band.shape, np.uint8)
    lo, hi = np.percentile(valid, [low, high])
    if hi <= lo:
        hi = lo + 1e-6
    return np.clip((np.nan_to_num(band, nan=lo) - lo) / (hi - lo), 0, 1).__mul__(255).astype(np.uint8)


def colorize(values: np.ndarray, low: float = -0.5, high: float = 0.6) -> np.ndarray:
    """把 NDWI 映射成蓝-灰-褐渐变，便于分辨水体和裸地。"""
    t = np.clip((np.nan_to_num(values, nan=low) - low) / (high - low), 0, 1)
    r = (200 - 140 * t).astype(np.uint8)
    g = (190 - 60 * t).astype(np.uint8)
    b = (150 + 105 * t).astype(np.uint8)
    return np.dstack([r, g, b])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="output/preview.png")
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--min-area-km2", type=float, default=0.02)
    ap.add_argument("--width", type=int, default=520, help="单幅图宽度（像素）")
    args = ap.parse_args()

    scene = gio.load_scene(args.input, S2_L2A_6BAND)
    ndwi_arr = ndwi(scene["green"], scene["nir"])
    ndsi_arr = ndsi(scene["green"], scene["swir1"])
    snow = masks.snow_mask(ndsi_arr, scene["nir"], 0.4)

    water = build_water_mask(ndwi_arr, args.threshold, exclude=snow)

    rgb = np.dstack([stretch(scene["red"]), stretch(scene["green"]), stretch(scene["blue"])])
    ndwi_rgb = colorize(ndwi_arr)

    # 在 NDWI 图上用红色描出提取结果
    edge = water & ~(
        np.roll(water, 1, 0) & np.roll(water, -1, 0) & np.roll(water, 1, 1) & np.roll(water, -1, 1)
    )
    ndwi_rgb[edge] = [255, 60, 60]

    panels = [Image.fromarray(rgb), Image.fromarray(ndwi_rgb)]
    h, w = panels[0].size[1], panels[0].size[0]
    scale = args.width / w
    panels = [p.resize((args.width, int(h * scale)), Image.BILINEAR) for p in panels]

    pad, top = 8, 26
    canvas = Image.new("RGB", (args.width * 2 + pad * 3, panels[0].size[1] + top + pad), (24, 24, 26))
    canvas.paste(panels[0], (pad, top))
    canvas.paste(panels[1], (pad * 2 + args.width, top))

    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 8), "true color (B04/B03/B02)", fill=(230, 230, 230))
    draw.text((pad * 2 + args.width, 8),
              f"NDWI  thr={args.threshold}  water={water.mean() * 100:.1f}%", fill=(230, 230, 230))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"已写出 {out}  ({canvas.size[0]}x{canvas.size[1]})")
    print(f"影像 {scene.shape[1]}x{scene.shape[0]}  像元 {scene.pixel_size_m:.0f} m")
    print(f"NDWI 范围 {np.nanmin(ndwi_arr):.3f} .. {np.nanmax(ndwi_arr):.3f}")
    print(f"水体占比 {water.mean() * 100:.2f}%   冰雪掩膜 {snow.mean() * 100:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
