"""克亚吉尔加密时序：在固定物候窗口内逐期观测，看面积怎么爬到平台值。

和 `kyagar_series.py` 的区别：
    那个是"每年一景"看年际趋势，采样日期会漂，混着季节相位；
    这个是"一年内逐期"看充水过程，时间轴是真实日期，能直接对上溃决时刻。

取数与判水规则都在 `glof/core/timeseries.py` 里，本脚本只负责
业务参数（坐标、PAD_DEG、平台值）和出图落表。

关键处理（实现在 core，这里只做说明）：
    * 同一日期可能在两个相邻瓦片各有一景（T43SFV / T43SGV），
      按**有效像元数**取覆盖更完整的那景，不能按云量。
    * 报告 valid_frac（窗口内有效像元占比）。云多的日期面积不可信，
      要能一眼看出来；但 valid_frac 对瓦片裁剪不敏感，必须同时看 valid_px。

用法：
    ./.venv/Scripts/python.exe scripts/kyagar_dense.py --year 2018
    ./.venv/Scripts/python.exe scripts/kyagar_dense.py --years 2017,2018
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
import matplotlib.dates as mdates  # noqa: E402
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
PAD_DEG = 0.03
PLATEAU_KM2 = 1.825  # 2025-10-26 年际序列测出的满蓄平台值

# CSV 列名与列序沿用改造前的输出，便于和历史结果逐列对比
CSV_FIELDS = [
    "tile",
    "cloud",
    "valid_px",
    "valid_frac",
    "area_km2",
    "water_frac",
    "scene",
    "date",
    "year",
]


def outburst_dates(db: str | Path) -> list[tuple[int, int, int]]:
    """从数据库取该点位有月日的溃决记录，没有月日就跳过。"""
    try:
        events, _ = ginv.load_glof_database(db)
    except Exception:
        return []
    out = []
    for e in events:
        if not e.year or abs(e.lat - KYAGAR_LAT) > 0.05 or abs(e.lon - KYAGAR_LON) > 0.05:
            continue
        try:
            month = int(float(e.month))
            day = int(float(e.day))
        except (TypeError, ValueError):
            continue
        out.append((e.year, month, day))
    return sorted(set(out))


def collect_year(year: int, start_md: str, end_md: str, max_cloud: float, limit: int):
    """跑一年，逐期打印并返回可直接写 CSV 的行。"""
    points = gts.build_series(
        BBOX,
        [year],
        start_md=start_md,
        end_md=end_md,
        max_cloud=max_cloud,
        per_year_limit=limit,
        progress=lambda msg: print(f"  {msg}"),
    )

    rows = []
    for p in points:
        print(
            f"  {p.date}  {p.tile:<7} 云 {p.cloud:5.1f}%  "
            f"有效 {p.valid_frac * 100:5.1f}%  面积 {p.area_km2:6.3f} km²"
        )
        rows.append(
            {
                "tile": p.tile,
                "cloud": p.cloud,
                "valid_px": p.valid_px,
                "valid_frac": p.valid_frac,
                "area_km2": p.area_km2,
                "water_frac": p.water_frac,
                "scene": p.scene,
                "date": p.date,
                "year": p.year,
            }
        )
    return rows


def main() -> int:
    global BBOX
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2018)
    ap.add_argument("--years", default=None, help="逗号分隔，优先于 --year")
    ap.add_argument("--start", default="06-01")
    ap.add_argument("--end", default="09-15")
    ap.add_argument("--max-cloud", type=float, default=60.0)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--db", default="data/raw/GLOF database of High Mountain Asia.zip")
    ap.add_argument("--out", default="output/kyagar")
    args = ap.parse_args()

    BBOX = [KYAGAR_LON - PAD_DEG, KYAGAR_LAT - PAD_DEG,
            KYAGAR_LON + PAD_DEG, KYAGAR_LAT + PAD_DEG]

    years = [int(y) for y in args.years.split(",")] if args.years else [args.year]
    events = outburst_dates(args.db)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for year in years:
        print(f"\n=== {year}  {args.start} → {args.end} ===")
        all_rows.extend(collect_year(year, args.start, args.end, args.max_cloud, args.limit))

    if not all_rows:
        print("没有取到任何有效观测。", file=sys.stderr)
        return 1

    csv_path = out_dir / "kyagar_dense.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(all_rows)

    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=140)
    colors = {2017: "#2980b9", 2018: "#c0392b", 2019: "#27ae60", 2016: "#8e44ad"}
    for year in years:
        pts = [r for r in all_rows if r["year"] == year]
        if not pts:
            continue
        dates = [np.datetime64(r["date"]) for r in pts]
        areas = [r["area_km2"] for r in pts]
        good = [r["valid_frac"] >= 0.6 for r in pts]
        ax.plot(dates, areas, "-", color=colors.get(year, "#555555"), lw=1.5, label=str(year))
        ax.scatter([d for d, g in zip(dates, good) if g],
                   [a for a, g in zip(areas, good) if g],
                   s=26, color=colors.get(year, "#555555"), zorder=3)
        ax.scatter([d for d, g in zip(dates, good) if not g],
                   [a for a, g in zip(areas, good) if not g],
                   s=26, facecolors="none", edgecolors=colors.get(year, "#555555"),
                   linewidths=1.2, zorder=3)

    ax.axhline(PLATEAU_KM2, color="#7f8c8d", ls="--", lw=1.0)
    ax.text(ax.get_xlim()[0], PLATEAU_KM2 + 0.05, "plateau 1.825 km²",
            fontsize=8, color="#7f8c8d")

    for yr, mo, dy in events:
        if yr in years:
            try:
                ax.axvline(np.datetime64(f"{yr}-{mo:02d}-{dy:02d}"),
                           color="#e67e22", ls=":", lw=1.4)
                ax.text(np.datetime64(f"{yr}-{mo:02d}-{dy:02d}"), ax.get_ylim()[1] * 0.92,
                        f" outburst {yr}-{mo:02d}-{dy:02d}", fontsize=7, color="#e67e22",
                        rotation=90, va="top")
            except Exception:
                pass

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.set_xlabel("date (within year)")
    ax.set_ylabel("water area (km²)")
    ax.set_title("Kyagar lake filling sequence (filled = valid_frac ≥ 60%, hollow = cloudy)")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, title="year")
    fig.tight_layout()
    png = out_dir / "kyagar_dense.png"
    fig.savefig(png)
    plt.close(fig)

    print(f"\n表格: {csv_path}")
    print(f"曲线: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
