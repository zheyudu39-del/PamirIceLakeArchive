"""克亚吉尔（Kyagar）冰坝湖面积时序。

为什么选它：ICIMOD 的 HMA GLOF 数据库里，这个湖有 **34 次溃决记录**（1880–2019），
是塔什库尔干县境内记录最密的点。它是冰坝湖——冰川前进阻塞克勒青河形成堰塞湖，
湖水蓄满后顶溃，所以面积会周期性涨落。**信号强、可验证**，是检验
"面积时序能不能抓到溃决前兆"的最佳样本。

做法：逐年在消融期（6–9 月）取覆盖最完整的一景，在固定窗口内算水体面积，
画成时序曲线。溃决年份标在图上。

取数与判水规则都在 `glof/core/timeseries.py` 里（按有效像元数选景、NDWI>0.02 且
NIR<0.20 判水），本脚本只保留业务参数与出图落表。

用法：
    ./.venv/Scripts/python.exe scripts/kyagar_series.py --start 2016 --end 2025
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from glof.core import inventory as ginv  # noqa: E402
from glof.core import timeseries as gts  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

KYAGAR_LAT, KYAGAR_LON = 35.677371, 77.190788
PAD_DEG = 0.03  # 约 3.3 km 半宽。太小会把堰塞湖截断，面积系统性偏小

# CSV 列名与列序沿用改造前的输出，便于和历史结果逐列对比
CSV_FIELDS = ["year", "date", "tile", "cloud", "valid_px", "area_km2", "ndwi_max", "scene"]


def known_events(db: str | Path) -> list[int]:
    try:
        events, _ = ginv.load_glof_database(db)
    except Exception:
        return []
    return sorted({e.year for e in events if e.year and abs(e.lat - KYAGAR_LAT) < 0.05
                   and abs(e.lon - KYAGAR_LON) < 0.05})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016")
    ap.add_argument("--end", default="2025")
    ap.add_argument("--max-cloud", type=float, default=25.0)
    ap.add_argument("--per-year", type=int, default=3, help="每年最多取几景")
    ap.add_argument("--db", default="data/raw/GLOF database of High Mountain Asia.zip")
    ap.add_argument("--out", default="output/kyagar")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    bbox = [KYAGAR_LON - PAD_DEG, KYAGAR_LAT - PAD_DEG,
            KYAGAR_LON + PAD_DEG, KYAGAR_LAT + PAD_DEG]

    events = known_events(args.db)
    print(f"数据库里该点位的溃决年份：{events if events else '未读到'}")

    records = []
    for year in range(int(args.start), int(args.end) + 1):
        points = gts.build_series(
            bbox,
            [year],
            start_md="06-01",
            end_md="09-30",
            max_cloud=args.max_cloud,
            per_year_limit=args.per_year,
            progress=lambda msg: print(f"  {msg}"),
        )
        if not points:
            continue

        # 关键：按"有效像元数"选景，不能只看云量。
        # 克亚吉尔正好压在 T43SFV / T43SGV 的边界上，
        # 云量最低的那景可能只覆盖一条 16 像元的窄缝，湖完全在窗口外。
        # 先比有效覆盖，再比面积：覆盖不足的景面积再大也是假的。
        best = max(points, key=lambda p: (p.valid_px, p.area_km2))

        records.append(
            {
                "year": year,
                "date": best.date,
                "tile": best.tile,
                "cloud": best.cloud,
                "valid_px": best.valid_px,
                "area_km2": best.area_km2,
                "ndwi_max": best.ndwi_max,
                "scene": best.scene,
            }
        )
        print(f"  {year}  {best.date}  云量 {best.cloud:4.1f}%  "
              f"面积 {best.area_km2:6.3f} km²")

    if not records:
        print("没有取到任何有效影像。", file=sys.stderr)
        return 1

    csv_path = out_dir / "kyagar_area_series.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(records)

    years = [r["year"] for r in records]
    areas = [r["area_km2"] for r in records]

    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=140)
    ax.plot(years, areas, "-o", color="#c0392b", lw=1.6, ms=5, label="extracted area")
    for yr in events:
        if int(args.start) <= yr <= int(args.end):
            ax.axvline(yr, color="#7f8c8d", ls=":", lw=0.9, alpha=0.8)
    if any(int(args.start) <= yr <= int(args.end) for yr in events):
        ax.plot([], [], color="#7f8c8d", ls=":", label="known outburst year (ICIMOD)")
    ax.set_xlabel("year")
    ax.set_ylabel("water area (km²)")
    ax.set_title("Kyagar ice-dammed lake, Jun–Sep water area")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    png = out_dir / "kyagar_area_series.png"
    fig.savefig(png)
    plt.close(fig)

    print(f"\n表格: {csv_path}")
    print(f"曲线: {png}")
    print(f"面积范围 {min(areas):.3f} – {max(areas):.3f} km²，"
          f"均值 {np.mean(areas):.3f} km²")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
