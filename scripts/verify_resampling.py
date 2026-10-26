"""验证逐波段重采样：把 SWIR 拆成独立的 20 m 文件，结果应与 10 m 单文件一致。

为什么需要这个测试：真实 Sentinel-2 L2A 的 B11/B12 是 20 m，而 B02–B04/B08 是 10 m。
`io.py` 现在会把 20 m 波段重采样到 10 m 参考网格。这一步如果写错，
NDSI 掩膜会整体错位，雪掩不住、湖被掩掉，但代码不会报任何错——只能靠测试发现。

测试构造：
    scene_10m.tif   6 波段，全 10 m（对照组）
    opt_10m.tif     blue/green/red/nir，10 m
    swir_20m.tif    swir1/swir2，20 m（由 10 m 数据 2x2 块平均得到）

断言：
    1. 两种读法的参考网格一致（形状、像元大小、仿射变换）
    2. NDWI 完全一致（来自同一批 10 m 波段，不应有任何差异）
    3. NDSI 只在雪/湖边界处有差异，中位差应接近 0
    4. 两种读法的湖泊数量与面积一致
    5. ref_resolution=20 时网格正确减半

用法：
    ./.venv/Scripts/python.exe scripts/verify_resampling.py
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import masks  # noqa: E402
from glof.core.indices import S2_L2A_4BAND, S2_L2A_6BAND, ndsi, ndwi  # noqa: E402
from glof.core.synthetic import make_synthetic_scene  # noqa: E402
from glof.core.water import extract_lakes  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def split_bands(src_path: Path, out_dir: Path) -> tuple[Path, Path]:
    """把 6 波段 10 m 影像拆成「光学 10 m」+「SWIR 20 m」两个文件。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        data = src.read()
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs

    # 波段顺序：blue, green, red, nir, swir1, swir2
    opt = data[0:4]
    swir = data[4:6]

    opt_path = out_dir / "opt_10m.tif"
    with rasterio.open(
        opt_path, "w", driver="GTiff", height=opt.shape[1], width=opt.shape[2],
        count=4, dtype=opt.dtype, crs=crs, transform=transform, compress="lzw",
    ) as dst:
        dst.write(opt)

    # 2x2 块平均模拟原生 20 m：只取每 2x2 块的第一行第一列会丢信息，平均更接近真实降采样
    h, w = swir.shape[1], swir.shape[2]
    swir20 = swir.reshape(2, h // 2, 2, w // 2, 2).mean(axis=(2, 4)).round().astype(swir.dtype)

    swir_path = out_dir / "swir_20m.tif"
    t20 = from_origin(transform.c, transform.f, abs(transform.a) * 2, abs(transform.e) * 2)
    with rasterio.open(
        swir_path, "w", driver="GTiff", height=swir20.shape[1], width=swir20.shape[2],
        count=2, dtype=swir20.dtype, crs=crs, transform=t20, compress="lzw",
    ) as dst:
        dst.write(swir20)

    return opt_path, swir_path


def main() -> int:
    out_dir = ROOT / "data" / "raw"
    scene_path = ROOT / "data" / "raw" / "synthetic_scene.tif"
    make_synthetic_scene(scene_path)

    print("=" * 74)
    print("步骤 1 / 拆分波段：光学 10 m + SWIR 20 m")
    print("=" * 74)
    opt_path, swir_path = split_bands(scene_path, out_dir)
    for p in (opt_path, swir_path):
        info = gio.describe_raster(p)
        print(f"  {p.name:<14} {info['size']:>12}  {info['count']} 波段  "
              f"像元 {info['pixel_size'][0]:.0f} m")

    print()
    print("=" * 74)
    print("步骤 2 / 两种读法的参考网格")
    print("=" * 74)
    ref = gio.load_scene(scene_path, S2_L2A_6BAND)
    split = gio.load_scene(
        {
            "blue": (opt_path, 1),
            "green": (opt_path, 2),
            "red": (opt_path, 3),
            "nir": (opt_path, 4),
            "swir1": (swir_path, 1),
            "swir2": (swir_path, 2),
        },
        S2_L2A_6BAND,
    )

    print(f"  单文件    : {ref.shape[1]}x{ref.shape[0]}  像元 {ref.pixel_size_m:.1f} m")
    print(f"  多文件    : {split.shape[1]}x{split.shape[0]}  像元 {split.pixel_size_m:.1f} m")
    resampled = split.resampled_bands()
    print(f"  被重采样  : " + "  ".join(f"{k} {v:.0f}m->{split.target_resolution:.0f}m"
                                        for k, v in resampled.items()))

    check("参考网格尺寸一致", split.shape == ref.shape, f"{split.shape} vs {ref.shape}")
    check("参考像元大小一致（10 m）", abs(split.pixel_size_m - 10.0) < 1e-6)
    check(
        "仿射变换一致（原点没有偏移）",
        np.allclose(list(split.transform)[:6], list(ref.transform)[:6], atol=1e-6),
    )
    check(
        "20 m 波段被识别为需要重采样",
        set(resampled) == {"swir1", "swir2"},
        f"实际 {sorted(resampled)}",
    )

    print()
    print("=" * 74)
    print("步骤 3 / 指数一致性")
    print("=" * 74)
    ndwi_ref = ndwi(ref["green"], ref["nir"])
    ndwi_split = ndwi(split["green"], split["nir"])
    check(
        "NDWI 完全一致（都来自同一批 10 m 波段）",
        np.allclose(ndwi_ref, ndwi_split, equal_nan=True),
    )

    ndsi_ref = ndsi(ref["green"], ref["swir1"])
    ndsi_split = ndsi(split["green"], split["swir1"])
    diff = np.abs(ndsi_ref - ndsi_split)
    diff = diff[np.isfinite(diff)]
    print(f"  NDSI 差异 : 中位 {np.median(diff):.5f}   95分位 {np.percentile(diff, 95):.5f}"
          f"   最大 {diff.max():.4f}")
    check(
        "NDSI 中位差异 < 0.01（边界插值误差，属预期）",
        float(np.median(diff)) < 0.01,
    )

    print()
    print("=" * 74)
    print("步骤 4 / 湖泊提取结果一致性")
    print("=" * 74)
    lakes_ref = extract_lakes(
        ndwi_ref, ref.transform, ref.crs, threshold=0.0, min_area_km2=0.01,
        exclude=masks.snow_mask(ndsi_ref, ref["nir"], 0.4),
    )
    lakes_split = extract_lakes(
        ndwi_split, split.transform, split.crs, threshold=0.0, min_area_km2=0.01,
        exclude=masks.snow_mask(ndsi_split, split["nir"], 0.4),
    )
    area_ref = sum(r.area_km2 for r in lakes_ref)
    area_split = sum(r.area_km2 for r in lakes_split)
    print(f"  单文件 : {len(lakes_ref)} 个湖，总面积 {area_ref:.4f} km2")
    print(f"  多文件 : {len(lakes_split)} 个湖，总面积 {area_split:.4f} km2")
    check("湖泊数量一致", len(lakes_ref) == len(lakes_split))
    check(
        "总面积差异 < 1%",
        abs(area_split - area_ref) / area_ref < 0.01,
        f"差异 {abs(area_split - area_ref) / area_ref * 100:.3f}%",
    )

    print()
    print("=" * 74)
    print("步骤 5 / ref_resolution=20 时网格减半")
    print("=" * 74)
    coarse = gio.load_scene(scene_path, S2_L2A_6BAND, ref_resolution=20.0)
    print(f"  尺寸 {coarse.shape[1]}x{coarse.shape[0]}  像元 {coarse.pixel_size_m:.1f} m")
    check("像元变为 20 m", abs(coarse.pixel_size_m - 20.0) < 1e-6)
    check("宽高各减半", coarse.shape == (ref.shape[0] // 2, ref.shape[1] // 2),
          f"{coarse.shape} vs 期望 {(ref.shape[0] // 2, ref.shape[1] // 2)}")
    check(
        "20 m 网格的像元面积是 10 m 的 4 倍",
        abs(coarse.pixel_area_m2 - 400.0) < 1e-6,
        f"{coarse.pixel_area_m2:.1f} m2",
    )

    print()
    print("=" * 74)
    print("步骤 6 / 缺 SWIR 时应给出明确报错")
    print("=" * 74)
    try:
        gio.load_scene(opt_path, S2_L2A_6BAND)
    except ValueError as exc:
        msg = str(exc)
        check("4 波段文件配 6 波段预设 -> 抛出 ValueError", True, msg[:60])
    else:
        check("4 波段文件配 6 波段预设 -> 抛出 ValueError", False, "没有报错")

    try:
        gio.load_scene(opt_path, S2_L2A_4BAND)
        check("4 波段文件配 4 波段预设 -> 正常读取", True)
    except Exception as exc:  # noqa: BLE001
        check("4 波段文件配 4 波段预设 -> 正常读取", False, repr(exc))

    print()
    print("=" * 74)
    print("步骤 7 / 多波段文件只给路径时应告警（默认只读第 1 波段）")
    print("=" * 74)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gio.load_scene(
            {
                "blue": (opt_path, 1),
                "green": opt_path,  # 故意只给路径，不指定波段号
                "red": (opt_path, 3),
                "nir": (opt_path, 4),
                "swir1": swir_path,
                "swir2": (swir_path, 2),
            },
            S2_L2A_6BAND,
        )
    hit = [w for w in caught if "第 1 波段" in str(w.message)]
    check("给出「默认读第 1 波段」警告", len(hit) >= 2, f"捕获 {len(hit)} 条")

    print()
    print("=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：{', '.join(FAILURES)}")
        return 1
    print("全部通过。逐波段重采样可用：20 m 的 B11 能正确对齐到 10 m 参考网格。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
