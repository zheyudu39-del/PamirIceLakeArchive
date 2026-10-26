"""最小闭环验证：合成影像 -> 湖泊多边形 -> 面积数字。

一条命令验证四件事：

    1. 整条链路能跑通（读栅格 -> 算指数 -> 掩膜 -> 矢量化 -> 面积）
    2. 面积算得对（与合成真值比对，误差应 <3%）
    3. MNDWI 与 NDSI 数值完全相同（同一公式），因此"MNDWI 提水 + NDSI 掩雪"自相矛盾
    4. 四种方案并排对比，能直接看出哪种配置是对的

用法：
    ./.venv/Scripts/python.exe scripts/verify_pipeline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import masks  # noqa: E402
from glof.core.indices import S2_L2A_6BAND, mndwi, ndsi, ndwi  # noqa: E402
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


def fmt(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}" if np.isfinite(value) else "-"


def disp_width(text: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in text)


def cell(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - disp_width(text))
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def run_config(label, scene, ndwi_arr, exclude, fill_holes, truth_main):
    records = extract_lakes(
        ndwi_arr,
        scene.transform,
        scene.crs,
        threshold=0.0,
        min_area_km2=0.01,
        exclude=exclude,
        fill_holes=fill_holes,
    )
    total = sum(r.area_km2 for r in records)
    main = records[0].area_km2 if records else 0.0
    err = (main - truth_main) / truth_main if truth_main else float("nan")
    return {
        "label": label,
        "n": len(records),
        "total": total,
        "main": main,
        "err": err,
        "records": records,
    }


def main() -> int:
    out_dir = ROOT / "output" / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 74)
    print("步骤 1 / 生成合成影像（1000x1000, 10 m, EPSG:32643, 6 波段）")
    print("=" * 74)
    truth = make_synthetic_scene(ROOT / "data" / "raw" / "synthetic_scene.tif")
    truth.to_json(out_dir / "synthetic_truth.json")
    truth_main_km2 = truth.lake_area_px_m2[0] / 1e6
    truth_total_km2 = sum(truth.lake_area_px_m2) / 1e6
    print(f"  影像      : {truth.path}")
    print(f"  真值主湖  : {fmt(truth_main_km2)} km2（像元计数）")
    print(f"  真值次湖  : {fmt(truth.lake_area_px_m2[1] / 1e6)} km2")
    print(f"  真值合计  : {fmt(truth_total_km2)} km2")
    print(f"  雪区面积  : {fmt(truth.snow_area_px_m2 / 1e6)} km2")
    print(f"  湖面浮冰  : {fmt(truth.floe_area_m2 / 1e6)} km2")

    scene = gio.load_scene(truth.path, S2_L2A_6BAND)
    ndwi_arr = ndwi(scene["green"], scene["nir"])
    ndsi_arr = ndsi(scene["green"], scene["swir1"])
    mndwi_arr = mndwi(scene["green"], scene["swir1"])

    print()
    print("=" * 74)
    print("步骤 2 / 验证 MNDWI 与 NDSI 是同一个公式")
    print("=" * 74)
    check(
        "mndwi() 与 ndsi() 逐像元完全相等",
        bool(np.allclose(mndwi_arr, ndsi_arr, equal_nan=True)),
    )
    water_ndsi = float(np.nanmedian(ndsi_arr[scene["nir"] < 0.05]))
    check(
        "水体像元的 NDSI 也大于 0.4（只按 NDSI 掩会掩掉整个湖）",
        water_ndsi > 0.4,
        f"水体 NDSI 中位数 = {water_ndsi:.3f}",
    )
    snow_ndwi = truth.expected_ndwi["snow"]
    check(
        "雪的理论 NDWI 也大于 0（只按 NDWI 提水会把雪提出来）",
        snow_ndwi > 0.0,
        f"雪 NDWI = {snow_ndwi:.3f}",
    )

    print()
    print("=" * 74)
    print("步骤 3 / 六种配置并排对比")
    print("=" * 74)

    snow = masks.snow_mask(ndsi_arr, scene["nir"], threshold=0.4)
    # 对照用：只按 NDSI，不带 NIR 条件（也就是很多资料里写的做法）
    snow_ndsi_only = np.isfinite(ndsi_arr) & (ndsi_arr > 0.4)

    results = [
        run_config("A. NDWI + 冰雪掩膜（推荐）", scene, ndwi_arr, snow, True, truth_main_km2),
        run_config("B. NDWI + 冰雪掩膜，不填洞", scene, ndwi_arr, snow, False, truth_main_km2),
        run_config("C. NDWI，无掩膜", scene, ndwi_arr, None, True, truth_main_km2),
        run_config("D. MNDWI，无掩膜", scene, mndwi_arr, None, True, truth_main_km2),
        run_config("E. MNDWI + 冰雪掩膜(带NIR)", scene, mndwi_arr, snow, True, truth_main_km2),
        run_config("F. MNDWI + 仅NDSI掩膜", scene, mndwi_arr, snow_ndsi_only, True, truth_main_km2),
    ]

    cols = [
        ("方案", 30, "left"),
        ("水体数", 8, "right"),
        ("总面积 km2", 13, "right"),
        ("主湖 km2", 11, "right"),
        ("相对误差", 11, "right"),
    ]
    print("  " + "".join(cell(n, w, a) for n, w, a in cols))
    print("  " + "-" * sum(w for _, w, _ in cols))
    for res in results:
        print(
            "  "
            + cell(res["label"], 30)
            + cell(str(res["n"]), 8, "right")
            + cell(f"{res['total']:.4f}", 13, "right")
            + cell(f"{res['main']:.4f}", 11, "right")
            + cell(f"{res['err'] * 100:+.1f}%", 11, "right")
        )

    print()
    print("=" * 74)
    print("步骤 4 / 断言")
    print("=" * 74)

    best = results[0]
    check("方案 A 水体数 = 2（两个湖，雪已被掩掉）", best["n"] == 2, f"实际 {best['n']}")
    check(
        "方案 A 主湖面积误差 < 3%",
        abs(best["err"]) < 0.03,
        f"实际 {best['err'] * 100:+.2f}%",
    )
    check(
        "方案 A 总面积误差 < 3%",
        abs(best["total"] - truth_total_km2) / truth_total_km2 < 0.03,
        f"实际 {best['total']:.4f} vs 真值 {truth_total_km2:.4f} km2",
    )
    check(
        "不填洞会低估主湖面积（浮冰变成洞）",
        results[1]["main"] < best["main"],
        f"{results[1]['main']:.4f} < {best['main']:.4f} km2",
    )
    check(
        "无冰雪掩膜时把雪当成水（总面积虚高）",
        results[2]["total"] > truth_total_km2 * 3,
        f"{results[2]['total']:.4f} km2 vs 真值 {truth_total_km2:.4f} km2",
    )
    check(
        "MNDWI 同样无法区分雪与湖",
        results[3]["total"] > truth_total_km2 * 3,
        f"{results[3]['total']:.4f} km2",
    )
    check(
        "掩膜带上 NIR 条件后，MNDWI 也能得到正确结果（说明关键在掩膜而非指数）",
        abs(results[4]["total"] - best["total"]) < 1e-6,
        f"E={results[4]['total']:.4f} vs A={best['total']:.4f} km2",
    )
    check(
        "只按 NDSI 掩膜时，MNDWI 提出的湖被自己的掩膜删光（0 个水体）",
        results[5]["n"] == 0,
        f"实际 {results[5]['n']} 个",
    )
    check(
        "多边形面积与像元计数面积一致（矢量化没丢面）",
        all(abs(r.area_check_ratio - 1.0) < 0.02 for r in best["records"]),
        f"比值 {[round(r.area_check_ratio, 3) for r in best['records']]}",
    )

    geo = gio.save_geojson(best["records"], out_dir / "lakes.geojson")
    csv_path = gio.save_csv(best["records"], out_dir / "lakes.csv")
    print()
    print(f"  结果已写出：{geo}")
    print(f"              {csv_path}")

    print()
    print("=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：{', '.join(FAILURES)}")
        return 1
    print("全部通过。闭环打通：一景 GeoTIFF -> 湖泊多边形 -> 面积数字。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
