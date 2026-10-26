"""风险分级验证：用克亚吉尔实测时序检验"面积/平台比值"判据。

为什么用真实数据而不是构造数据
------------------------------
这条判据的全部价值在于"评委可以自己验算"。所以验证也必须落在真实观测上：
`output/kyagar/kyagar_dense.csv` 是 2018 年溃决前后 13 期 Sentinel-2 面积，
`output/kyagar/2017/kyagar_dense.csv` 是 2017 年 5 期。

验证的六件事：

    1. 平台值能从时序里**自动估出来**（不硬编码 1.825），且跨年复现
    2. 溃决前 4 天（2018-08-06，1.825 km²）判 HIGH
    3. 溃决次日（2018-08-11，0.645 km²）判 LOW
    4. 充水初期（2018-06-02，0.155 km²）判 LOW
    5. 缺数据时判据显示 UNKNOWN、reasons 写明"缺数据未评估"，**不会静默判低**
    6. 阈值 ±20% 扰动下的等级翻转率，以及风险字段能落进 GeoJSON / CSV

第 2–4 条的判据用的是**留出法**：评估 2018 年的湖，平台值由 2017 年序列估。
若把待评估那一期也塞进序列，最大值就是它自己，比值恒等于 1——那是循环论证。
脚本第 2 步专门把这个坑跑出来给评委看。

用法：
    ./.venv/Scripts/python.exe scripts/verify_risk.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glof.core import io as gio  # noqa: E402
from glof.core.risk import (  # noqa: E402
    RiskLevel,
    assess_ice_dammed,
    assess_lake,
    assess_moraine_dammed,
    estimate_plateau_area,
    sensitivity,
)
from glof.core.water import LakeRecord  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []

DENSE_2018 = ROOT / "output" / "kyagar" / "kyagar_dense.csv"
DENSE_2017 = ROOT / "output" / "kyagar" / "2017" / "kyagar_dense.csv"

# 实测平台值，仅用于"断言估出来的值接近它"，绝不参与判定
PLATEAU_TRUTH = 1.825


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def disp_width(text: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in text)


def cell(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - disp_width(text))
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def read_series(path: Path) -> list[tuple[str, float]]:
    """读 (日期, 面积 km²)。T43SGV 窄条窗口的观测面积系统性偏小，但本脚本
    只做判据逻辑验证，不比较年际趋势，因此全部保留并在表里照原样打印。"""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [(r["date"], float(r["area_km2"])) for r in csv.DictReader(fh)]
    return sorted(rows)


def main() -> int:
    out_dir = ROOT / "output" / "verify_risk"
    out_dir.mkdir(parents=True, exist_ok=True)

    for path in (DENSE_2018, DENSE_2017):
        if not path.exists():
            print(f"缺少实测数据 {path}，无法验证。先跑 scripts/kyagar_dense.py。")
            return 1

    series_2018 = read_series(DENSE_2018)
    series_2017 = read_series(DENSE_2017)
    areas_2018 = [a for _, a in series_2018]

    print("=" * 74)
    print("步骤 1 / 从实测时序自动估平台面积（不硬编码）")
    print("=" * 74)
    plateau_2018 = estimate_plateau_area(areas_2018)
    plateau_2017 = estimate_plateau_area([a for _, a in series_2017])
    p90 = estimate_plateau_area(areas_2018, quantile=0.90)
    print(f"  2018 序列 {len(areas_2018)} 期，最大值估计平台 = {plateau_2018:.4f} km²")
    print(f"  2017 序列 {len(series_2017)} 期，最大值估计平台 = {plateau_2017:.4f} km²")
    print(f"  对照：90% 分位 = {p90:.4f} km²（比最大值低 {(1 - p90 / plateau_2018) * 100:.1f}%）")
    print("  取最大值而非分位数的理由：平台是物理天花板，湖不会超过它，")
    print("  所以观测上包络才是无偏估计；取分位数会低估平台、抬高比值、造成误报。")

    check(
        "2018 序列自动估出的平台值接近实测 1.825 km²（误差 <1%）",
        plateau_2018 is not None and abs(plateau_2018 - PLATEAU_TRUTH) / PLATEAU_TRUTH < 0.01,
        f"估出 {plateau_2018:.4f}，偏差 {(plateau_2018 - PLATEAU_TRUTH) / PLATEAU_TRUTH * 100:+.2f}%",
    )
    check(
        "平台值跨年复现（2017 与 2018 两次独立观测相差 <0.2%）",
        plateau_2017 is not None
        and abs(plateau_2017 - plateau_2018) / plateau_2018 < 0.002,
        f"{plateau_2017:.4f} vs {plateau_2018:.4f}，差 {abs(plateau_2017 - plateau_2018):.4f} km²",
    )
    check(
        "分位数估计会低估平台（说明为什么不用 90% 分位）",
        p90 < plateau_2018 * 0.95,
        f"90% 分位 {p90:.4f} < 最大值 {plateau_2018:.4f}",
    )
    check(
        "少于 3 期观测时拒绝估平台（单期最大值就是它自己，不可信）",
        estimate_plateau_area([1.0, 1.8]) is None,
        "2 期输入返回 None",
    )

    print()
    print("=" * 74)
    print("步骤 2 / 为什么必须用留出法（把待评估那一期算进序列会循环论证）")
    print("=" * 74)
    self_included = assess_ice_dammed(
        plateau_2018, reference_areas=areas_2018, label="2018-08-06(自包含)"
    )
    ratio_self = self_included.criteria[0].value
    print(f"  自包含序列：平台 = {plateau_2018:.4f}，当前面积 = {plateau_2018:.4f}，比值 = {ratio_self:.4f}")
    print("  比值恒为 1 → 判据必然命中 → 这不是证据，是恒等式。")
    print("  所以下面所有判定都用另一年（或本次观测之前）的序列估平台。")
    check(
        "自包含序列的比值恒为 1（据此说明本脚本采用留出法）",
        abs(ratio_self - 1.0) < 1e-9,
        f"比值 {ratio_self:.6f}",
    )

    print()
    print("=" * 74)
    print("步骤 3 / 逐期判定（平台值取 2017 年估计值，留出法）")
    print("=" * 74)
    cols = [("日期", 12, "left"), ("面积 km²", 11, "right"), ("比值", 9, "right"), ("等级", 10, "left")]
    print("  " + " ".join(cell(n, w, a) for n, w, a in cols))
    print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))

    assessed: dict[str, object] = {}
    for date, area in series_2018:
        res = assess_ice_dammed(area, plateau_area_km2=plateau_2017, label=date)
        assessed[date] = res
        ratio = res.criteria[0].value
        print(
            "  "
            + " ".join(
                (
                    cell(date, 12),
                    cell(f"{area:.4f}", 11, "right"),
                    cell(f"{ratio:.4f}", 9, "right"),
                    cell(res.level.value, 10),
                )
            )
        )

    print()
    pre = assessed["2018-08-06"]
    post = assessed["2018-08-11"]
    early = assessed["2018-06-02"]
    print(f"  溃决前 4 天 2018-08-06：{pre.reasons[0]}")
    print(f"  溃决次日   2018-08-11：{post.reasons[0]}")
    print(f"  充水初期   2018-06-02：{early.reasons[0]}")

    check(
        "2018-08-06（1.825 km²，平台值）判 HIGH",
        pre.level is RiskLevel.HIGH,
        f"实际 {pre.level.value}，比值 {pre.criteria[0].value:.4f}",
    )
    check(
        "2018-08-11（0.645 km²，溃决后）判 LOW",
        post.level is RiskLevel.LOW,
        f"实际 {post.level.value}，比值 {post.criteria[0].value:.4f}",
    )
    check(
        "2018-06-02（0.155 km²，初期）判 LOW",
        early.level is RiskLevel.LOW,
        f"实际 {early.level.value}，比值 {early.criteria[0].value:.4f}",
    )
    check(
        "2018-07-22（1.567 km²，接近但未达平台）判 LOW——判据不是「湖大就危险」",
        assessed["2018-07-22"].level is RiskLevel.LOW,
        f"比值 {assessed['2018-07-22'].criteria[0].value:.4f} < 0.95",
    )

    res_2017 = assess_ice_dammed(
        dict(series_2017)["2017-07-27"], plateau_area_km2=plateau_2018, label="2017-07-27"
    )
    check(
        "反向留出：2017-07-27（1.8248 km²）用 2018 平台值判 HIGH（平台跨年可复现）",
        res_2017.level is RiskLevel.HIGH,
        f"比值 {res_2017.criteria[0].value:.4f}",
    )

    print()
    print("=" * 74)
    print("步骤 4 / 冰碛坝判据：缺数据必须显示 UNKNOWN，不能静默通过")
    print("=" * 74)

    partial = assess_moraine_dammed(
        area_km2=0.80,
        baseline_area_km2=0.50,
        baseline_year=2015,
        year=2025,
        label="构造:只填了扩张速率",
    )
    print("  只提供面积时序（无 DEM、无冰川编目）：")
    for line in partial.reasons:
        print(f"    {line}")
    print(f"    → {partial.summary}")

    slope_crit = next(c for c in partial.criteria if c.name == "坝体下游坡度")
    glacier_crit = next(c for c in partial.criteria if c.name == "母冰川接触")
    check(
        "下游坡度缺数据时判据为 UNKNOWN（hit is None，不是 False）",
        slope_crit.hit is None and slope_crit.status == "UNKNOWN",
        f"status={slope_crit.status}",
    )
    check(
        "母冰川接触缺数据时判据为 UNKNOWN",
        glacier_crit.hit is None,
        f"status={glacier_crit.status}",
    )
    check(
        "reasons 里明确写出「缺数据未评估」",
        sum("缺数据未评估" in r for r in partial.reasons) == 2,
        f"命中 {sum('缺数据未评估' in r for r in partial.reasons)} 条",
    )
    check(
        "有判据命中但仍有缺口时，最高只到 MEDIUM（不能凭缺数据升到 HIGH）",
        partial.level is RiskLevel.MEDIUM,
        f"实际 {partial.level.value}，命中 {partial.n_hit} 条、缺 {partial.n_unknown} 条",
    )

    no_hit_partial = assess_moraine_dammed(
        area_km2=0.51, baseline_area_km2=0.50, baseline_year=2015, year=2025
    )
    check(
        "一条未命中但仍有缺口时判 UNKNOWN，而不是 LOW（不把「不知道」当「安全」）",
        no_hit_partial.level is RiskLevel.UNKNOWN,
        f"实际 {no_hit_partial.level.value}，缺 {no_hit_partial.n_unknown} 条",
    )

    complete = assess_moraine_dammed(
        area_km2=0.80,
        baseline_area_km2=0.50,
        baseline_year=2015,
        year=2025,
        downstream_slope_deg=12.0,
        glacier_contact=True,
    )
    check(
        "三条判据数据齐全且全部命中时判 HIGH（规则本身能用）",
        complete.level is RiskLevel.HIGH and complete.n_unknown == 0,
        f"实际 {complete.level.value}，命中 {complete.n_hit}/3",
    )

    no_plateau = assess_ice_dammed(0.5)
    check(
        "冰坝湖没有平台值也没有历史序列时判 UNKNOWN 并说明原因",
        no_plateau.level is RiskLevel.UNKNOWN and "未提供平台面积" in no_plateau.reasons[0],
        no_plateau.reasons[0][:46] + "…",
    )

    unknown_type = assess_lake("堰塞湖", 0.3)
    check(
        "坝型无法识别时不猜，直接 UNKNOWN",
        unknown_type.level is RiskLevel.UNKNOWN and unknown_type.dam_type == "unknown",
        unknown_type.summary[:30] + "…",
    )

    print()
    print("=" * 74)
    print("步骤 5 / 阈值 ±20% 敏感性分析")
    print("=" * 74)

    moraine_cases = [
        dict(
            lake_type="moraine", area_km2=0.80, baseline_area_km2=0.50,
            baseline_year=2015, year=2025,
            downstream_slope_deg=12.0, glacier_contact=True, label="构造:数据齐全",
        ),
        dict(
            lake_type="moraine", area_km2=0.605, baseline_area_km2=0.50,
            baseline_year=2015, year=2025,
            downstream_slope_deg=9.0, glacier_contact=False, label="构造:阈值附近",
        ),
        dict(
            lake_type="moraine", area_km2=0.51, baseline_area_km2=0.50,
            baseline_year=2015, year=2025, label="构造:数据不全",
        ),
    ]
    cases = [
        dict(lake_type="ice", area_km2=area, plateau_area_km2=plateau_2017, label=date)
        for date, area in series_2018
    ] + moraine_cases

    shifts = sensitivity(cases, perturbation=0.2)
    cols = [
        ("阈值", 24, "left"),
        ("基准", 9, "right"),
        ("-20%", 9, "right"),
        ("翻转率", 9, "right"),
        ("+20%", 9, "right"),
        ("翻转率", 9, "right"),
    ]
    print("  " + " ".join(cell(n, w, a) for n, w, a in cols))
    print("  " + "-" * (sum(w for _, w, _ in cols) + len(cols) - 1))
    for s in shifts:
        print(
            "  "
            + " ".join(
                (
                    cell(s.name, 24),
                    cell(f"{s.base:g}", 9, "right"),
                    cell(f"{s.low:g}", 9, "right"),
                    cell(f"{s.flip_rate_low * 100:.1f}%", 9, "right"),
                    cell(f"{s.high:g}", 9, "right"),
                    cell(f"{s.flip_rate_high * 100:.1f}%", 9, "right"),
                )
            )
        )
    print(f"  样本 {len(cases)} 个（13 期克亚吉尔实测 + 3 个构造冰碛坝用例）")
    for s in shifts:
        if s.flipped_high:
            print(f"  {s.name} +20% 翻转：{', '.join(s.flipped_high)}")
        if s.flipped_low:
            print(f"  {s.name} -20% 翻转：{', '.join(s.flipped_low)}")

    worst = max(s.max_flip_rate for s in shifts)
    check("敏感性分析覆盖 3 个阈值", len(shifts) == 3, f"{[s.name for s in shifts]}")
    check("每个阈值都跑了全部 16 个样本", all(s.n_cases == len(cases) for s in shifts))
    check(
        "平台比值阈值 +20% 时，2018-08-06 的 HIGH 会翻转（说明该判据确实敏感）",
        "2018-08-06" in next(s for s in shifts if s.name == "plateau_ratio").flipped_high,
        f"翻转 {next(s for s in shifts if s.name == 'plateau_ratio').flipped_high}",
    )
    check(
        "阈值 ±20% 内最大等级翻转率 < 25%（结论不依赖阈值精调）",
        worst < 0.25,
        f"最大翻转率 {worst * 100:.1f}%",
    )

    print()
    print("=" * 74)
    print("步骤 6 / 风险字段落进 GeoJSON 与 CSV")
    print("=" * 74)

    # 占位几何：这里只验证字段能否完整落盘，几何本身不代表真实湖形
    placeholder = json.dumps(
        {
            "type": "Polygon",
            "coordinates": [[
                [77.1850, 35.6700], [77.1960, 35.6700],
                [77.1960, 35.6800], [77.1850, 35.6800], [77.1850, 35.6700],
            ]],
        }
    )
    records = []
    for idx, (date, area) in enumerate(
        [("2018-08-06", 1.8254), ("2018-08-11", 0.6448), ("2018-06-02", 0.1551)], start=1
    ):
        res = assess_ice_dammed(area, plateau_area_km2=plateau_2017, label=date)
        records.append(
            LakeRecord(
                lake_id=idx,
                area_m2=area * 1e6,
                perimeter_m=0.0,
                n_pixels=0,
                area_px_m2=area * 1e6,
                mean_ndwi=float("nan"),
                centroid_lon=77.1908,
                centroid_lat=35.6774,
                geometry_wgs84_json=placeholder,
                risk_level=res.level.value,
                risk_reasons=res.reasons,
            )
        )

    geo_path = gio.save_geojson(records, out_dir / "risk_lakes.geojson")
    csv_path = gio.save_csv(records, out_dir / "risk_lakes.csv")
    payload = json.loads(geo_path.read_text(encoding="utf-8"))
    props = [f["properties"] for f in payload["features"]]
    levels = [p["risk_level"] for p in props]
    check(
        "GeoJSON properties 带 risk_level，且与判定一致",
        levels == ["HIGH", "LOW", "LOW"],
        f"{levels}",
    )
    check(
        "GeoJSON properties 带 risk_reasons 且是字符串列表",
        all(isinstance(p["risk_reasons"], list) and p["risk_reasons"] for p in props),
        f"{len(props[0]['risk_reasons'])} 条判据文本",
    )
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    check(
        "CSV 表头带 risk_level / risk_reasons，内容非空",
        rows and "risk_level" in rows[0] and rows[0]["risk_reasons"] != "",
        f"risk_level={rows[0]['risk_level']}",
    )
    print(f"  结果已写出：{geo_path}")
    print(f"              {csv_path}")

    print()
    print("=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：{', '.join(FAILURES)}")
        return 1
    print("全部通过。风险分级可用：冰坝看面积/平台比值，冰碛坝缺数据时如实标 UNKNOWN。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
