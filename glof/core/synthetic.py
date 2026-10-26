"""合成测试影像生成器。

**仅用于测试与演示，不是真实数据。**

为什么需要它：真实 Sentinel 影像需要联网、几百 MB、而且"正确答案"要靠人工目视解译，
每次改代码都得重新判读一遍。这个生成器造出一景"已知答案"的假影像，
让整条链路可以离线、秒级、可重复地验证——改完算法跑一次就知道有没有退步。

场景里刻意埋了三个陷阱，正好对应真实高原数据的三个难点：

    1. 大片雪/冰（NDSI 高，但 NDWI 也略大于 0）——只有冰雪掩膜能救
    2. 湖面浮冰（水中间一块冰）——不填洞会低估面积
    3. 冰雪与湖泊的光谱差异极小——阈值稍微一松就混

反射率取值参照 Sentinel-2 L2A 典型地物（0–1）：
    水体  NIR 极低、SWIR 更低
    雪冰  Green/NIR 都很高、SWIR 很低
    裸地  NIR 高于 Green，SWIR 高
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

__all__ = ["SyntheticTruth", "make_synthetic_scene"]

BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")

# 典型地物反射率（Sentinel-2 波段口径）
LAND = {"blue": 0.090, "green": 0.110, "red": 0.140, "nir": 0.330, "swir1": 0.270, "swir2": 0.160}
WATER = {"blue": 0.065, "green": 0.055, "red": 0.032, "nir": 0.010, "swir1": 0.008, "swir2": 0.004}
SNOW = {"blue": 0.900, "green": 0.880, "red": 0.850, "nir": 0.780, "swir1": 0.100, "swir2": 0.050}


@dataclass
class SyntheticTruth:
    """生成影像的"标准答案"，供验证脚本断言。"""

    path: Path
    epsg: int
    pixel_size: float
    lake_area_px_m2: list[float]
    lake_area_analytic_m2: list[float]
    snow_area_px_m2: float
    floe_area_m2: float
    expected_ndwi: dict[str, float]

    def to_json(self, out: str | Path) -> Path:
        out = Path(out)
        payload = asdict(self)
        payload["path"] = str(self.path)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out


def make_synthetic_scene(
    path: str | Path,
    *,
    size: int = 1000,
    pixel_size: float = 10.0,
    epsg: int = 32643,
    origin: tuple[float, float] = (500_000.0, 4_200_000.0),
    seed: int = 42,
    noise: float = 0.02,
) -> SyntheticTruth:
    """生成一景 6 波段（B02,B03,B04,B08,B11,B12）的合成影像。

    默认 1000×1000 像元、10 m 分辨率，覆盖约 10 km × 10 km，
    坐标系 EPSG:32643（UTM 43N，塔什库尔干所在带）。
    """
    rng = np.random.default_rng(seed)
    h = w = size
    transform = from_origin(origin[0], origin[1], pixel_size, pixel_size)

    x = origin[0] + (np.arange(w) + 0.5) * pixel_size
    y = origin[1] - (np.arange(h) + 0.5) * pixel_size
    xx, yy = np.meshgrid(x, y)

    cx = origin[0] + size * pixel_size / 2.0
    cy = origin[1] - size * pixel_size / 2.0

    def circle(ox: float, oy: float, radius: float) -> np.ndarray:
        return (xx - ox) ** 2 + (yy - oy) ** 2 <= radius**2

    lake1 = circle(cx - 1200.0, cy + 900.0, 300.0)
    lake2 = circle(cx + 1800.0, cy - 1400.0, 80.0)
    snow_patch = (np.abs(xx - (cx + 2600.0)) <= 1000.0) & (np.abs(yy - (cy + 1800.0)) <= 700.0)
    floe = circle(cx - 1140.0, cy + 860.0, 40.0)

    refl = {name: np.full((h, w), LAND[name], dtype=np.float32) for name in BANDS}
    for name in BANDS:
        refl[name][lake1 | lake2] = WATER[name]
        refl[name][snow_patch] = SNOW[name]
        refl[name][floe] = SNOW[name]

    if noise > 0:
        for name in BANDS:
            refl[name] = np.clip(
                refl[name] * (1.0 + noise * rng.standard_normal((h, w))), 0.0, 1.0
            )

    stack = np.stack([np.rint(refl[name] * 10000.0).astype(np.uint16) for name in BANDS])

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=h,
        width=w,
        count=len(BANDS),
        dtype="uint16",
        crs=f"EPSG:{epsg}",
        transform=transform,
        compress="lzw",
    ) as dst:
        dst.write(stack)
        for idx, name in enumerate(BANDS, start=1):
            dst.set_band_description(idx, name)

    pixel_area = pixel_size * pixel_size

    def ndwi_of(values: dict[str, float]) -> float:
        return (values["green"] - values["nir"]) / (values["green"] + values["nir"])

    return SyntheticTruth(
        path=path,
        epsg=epsg,
        pixel_size=pixel_size,
        lake_area_px_m2=[float(lake1.sum() * pixel_area), float(lake2.sum() * pixel_area)],
        lake_area_analytic_m2=[float(np.pi * 300.0**2), float(np.pi * 80.0**2)],
        snow_area_px_m2=float(snow_patch.sum() * pixel_area),
        floe_area_m2=float(floe.sum() * pixel_area),
        expected_ndwi={
            "land": ndwi_of(LAND),
            "water": ndwi_of(WATER),
            "snow": ndwi_of(SNOW),
        },
    )
