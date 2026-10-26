"""验证 risk_series：时序读入 → 留出法参照 → 逐期判定 → CLI 落盘。

检验的是"从一条 CSV 到每期风险等级"这条链路的口径正确性，
判据本身的规则在 scripts/verify_risk.py 里验过，这里不重复。

数据用的是真实产物：output/kyagar/kyagar_area_series.csv（2016–2025 年际，
含 2017-07-27 与 2018-08-06 两个满蓄期）与 kyagar_dense.csv（2018 年 13 期加密）。
已知事实（docs §7.9/§7.10、verify_risk 实测）：
    平台值 ≈ 1.825 km²，2017 与 2018 两次独立观测相差 0.03%；
    2018-08-06（1.8254 km²）是溃决前 4 天的满蓄状态，应当判 HIGH。

运行：
    ./.venv/Scripts/python.exe scripts/verify_risk_series.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glof.cli import main as cli_main  # noqa: E402
from glof.core import risk as grisk  # noqa: E402
from glof.core import risk_series as grs  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ANNUAL = ROOT / "output" / "kyagar" / "kyagar_area_series.csv"
DENSE = ROOT / "output" / "kyagar" / "kyagar_dense.csv"

n_pass = 0
n_all = 0


def check(text: str, ok: bool, detail: str = "") -> None:
    global n_pass, n_all
    n_all += 1
    mark = "PASS" if ok else "FAIL"
    if ok:
        n_pass += 1
    suffix = f"  {detail}" if detail else ""
    print(f"  [{mark}] {text}{suffix}")


def main() -> int:
    print("=" * 74)
    print("步骤 1 / 读 CSV：两种列格式 + BOM + 缺列容错")
    print("=" * 74)
    annual = grs.read_series_csv(ANNUAL)
    check("年际 CSV 读出 9 期且按日期升序", len(annual) == 9 and annual == sorted(annual, key=lambda p: p.date),
          f"{len(annual)} 期，{annual[0].date} → {annual[-1].date}")
    full_2018 = next(p for p in annual if p.date == "2018-08-06")
    check("2018-08-06 面积 1.8254", abs(full_2018.area_km2 - 1.8254) < 1e-9,
          f"{full_2018.area_km2}")
    check("年际 CSV 没有 valid_frac 列 → nan（不猜）",
          full_2018.valid_frac != full_2018.valid_frac and full_2018.ndwi_max == full_2018.ndwi_max)

    dense = grs.read_series_csv(DENSE)
    check("加密 CSV 读出 13 期（列序不同、缺 ndwi_max 列）", len(dense) == 13, f"{len(dense)} 期")
    check("加密 CSV 的 valid_frac 有值（1.0）", dense[0].valid_frac == 1.0)

    print()
    print("=" * 74)
    print("步骤 2 / 留出法参照：跨年优先、年内回退、无参照如实为空")
    print("=" * 74)
    ref, policy = grs.holdout_reference_areas(annual, "2018-08-06")
    plateau = grisk.estimate_plateau_area(ref)
    check("2018 的参照用跨年 8 期，平台 = 2017 的 1.8248",
          len(ref) == 8 and abs((plateau or 0) - 1.8248) < 1e-9,
          f"{policy}；平台 {plateau}")
    ref_one_year, policy_one_year = grs.holdout_reference_areas(dense, "2018-08-06")
    check("加密序列只有 2018 一年 → 年内回退（早期 7 期）",
          len(ref_one_year) == 7 and "年内回退" in policy_one_year,
          f"{policy_one_year}")
    ref_earliest, policy_earliest = grs.holdout_reference_areas(dense, "2018-06-02")
    check("最早的期没有更早观测 → 空参照（上游将判 UNKNOWN）",
          ref_earliest == [] and "无可用参照" in policy_earliest, policy_earliest)

    print()
    print("=" * 74)
    print("步骤 3 / 冰坝逐期判定（年际序列，全期）")
    print("=" * 74)
    results = grs.assess_series(annual, "ice")
    by_date = {r.date: r for r in results}
    r2018 = by_date["2018-08-06"]
    r2017 = by_date["2017-07-27"]
    r2016 = by_date["2016-06-27"]
    check("2018-08-06（溃决前 4 天满蓄）判 HIGH，比值 1.0003",
          r2018.assessment.level is grisk.RiskLevel.HIGH and abs((r2018.value or 0) - 1.0003) < 5e-4,
          f"{r2018.assessment.level.value}，比值 {r2018.value:.4f}")
    check("2017-07-27（满蓄）判 HIGH，比值 0.9997",
          r2017.assessment.level is grisk.RiskLevel.HIGH and abs((r2017.value or 0) - 0.9997) < 5e-4,
          f"{r2017.assessment.level.value}，比值 {r2017.value:.4f}")
    check("2016-06-27（窄条小面积）判 LOW——平台取 max 不受窄条污染",
          r2016.assessment.level is grisk.RiskLevel.LOW,
          f"{r2016.assessment.level.value}，比值 {r2016.value:.4f}")
    n_high = sum(1 for r in results if r.assessment.level is grisk.RiskLevel.HIGH)
    check("全期只有 2017/2018 两个满蓄年 HIGH", n_high == 2, f"HIGH {n_high} 期")

    dense_results = grs.assess_series(dense, "ice")
    d0806 = next(r for r in dense_results if r.date == "2018-08-06")
    d0602 = next(r for r in dense_results if r.date == "2018-06-02")
    check("加密序列 2018-08-06 年内回退仍判 HIGH（比值 1.165，政策说明里写了虚高风险）",
          d0806.assessment.level is grisk.RiskLevel.HIGH and "年内回退" in d0806.policy,
          f"{d0806.assessment.level.value}，比值 {d0806.value:.4f}")
    check("加密序列最早一期如实 UNKNOWN（无参照可留出）",
          d0602.assessment.level is grisk.RiskLevel.UNKNOWN and "不足 3 期" in d0602.assessment.reasons[0],
          d0602.assessment.level.value)

    print()
    print("=" * 74)
    print("步骤 4 / 冰碛坝：基准期政策 + 缺数据诚实记账")
    print("=" * 74)
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "moraine.csv"
        fake.write_text(
            "year,date,area_km2,valid_px,valid_frac,tile,cloud,scene\n"
            "2015,2015-07-01,0.50,1000,1.0,T,X,0,s\n"
            "2025,2025-07-01,0.80,1000,1.0,T,X,0,s\n",
            encoding="utf-8",
        )
        moraine = grs.read_series_csv(fake)
        check("合成冰碛坝 CSV 读出 2 期", len(moraine) == 2)
        mres = grs.assess_series(moraine, "moraine")
        m2025 = next(r for r in mres if r.date == "2025-07-01")
        m2015 = next(r for r in mres if r.date == "2015-07-01")
        check("2025 对 2015 基准：6%/年 命中，但缺坡度/冰川接触 → 最高 MEDIUM",
              m2025.assessment.level is grisk.RiskLevel.MEDIUM and m2025.assessment.n_unknown == 2,
              f"{m2025.assessment.level.value}，命中 {m2025.assessment.n_hit}、缺 {m2025.assessment.n_unknown}")
        check("基准期当期（2015）时间跨度 0 → 判 UNKNOWN",
              m2015.assessment.level is grisk.RiskLevel.UNKNOWN,
              m2015.assessment.level.value)
        # 数据齐全时规则本身能给出 HIGH
        m_full = grisk.assess_moraine_dammed(
            0.80, baseline_area_km2=0.50, baseline_year=2015, year=2025,
            downstream_slope_deg=12.0, glacier_contact=True,
        )
        check("三条判据齐全且全命中时判 HIGH（规则本身能用，与 verify_risk 一致）",
              m_full.level is grisk.RiskLevel.HIGH, m_full.level.value)

    print()
    print("=" * 74)
    print("步骤 5 / 敏感性分析与 CLI 端到端")
    print("=" * 74)
    cases = grs.series_sensitivity_cases(annual, "ice")
    shifts = grisk.sensitivity(cases, perturbation=0.2)
    plateau_row = next(s for s in shifts if s.name == "plateau_ratio")
    check("敏感性覆盖 9 个跨年用例、3 个阈值", len(cases) == 9 and len(shifts) == 3,
          f"用例 {len(cases)}，阈值 {len(shifts)}")
    check("plateau_ratio +20%（0.95→1.14）翻转两个满蓄年；-20%（0.76）零翻转（序列里没有比值落在 0.76–0.95 的期）",
          set(plateau_row.flipped_high) == {"2017-07-27", "2018-08-06"} and plateau_row.flipped_low == [],
          f"-20% 翻 {plateau_row.flipped_low}，+20% 翻 {plateau_row.flipped_high}")

    out_dir = ROOT / "output" / "risk"
    code = cli_main([
        "risk", "--input", str(ANNUAL), "--lake-type", "ice",
        "--sensitivity", "--out", str(out_dir),
    ])
    check("CLI risk 子命令退出码 0", code == 0, f"exit {code}")
    js = json.loads((out_dir / "risk_assessments.json").read_text(encoding="utf-8"))
    check("JSON 落盘：9 行、含阈值与判据文本",
          len(js["rows"]) == 9 and abs(js["thresholds"]["plateau_ratio"] - 0.95) < 1e-9
          and all(row["reasons"] for row in js["rows"]),
          f"rows {len(js['rows'])}，plateau_ratio {js['thresholds']['plateau_ratio']}")
    check("阈值覆盖能进 CLI（--threshold plateau_ratio=1.2 后 HIGH 消失）",
          cli_main(["risk", "--input", str(ANNUAL), "--lake-type", "ice",
                    "--threshold", "plateau_ratio=1.2", "--out", str(out_dir)]) == 0
          and all(row["level"] != "HIGH"
                  for row in json.loads((out_dir / "risk_assessments.json").read_text(encoding="utf-8"))["rows"]),
          "plateau_ratio=1.2 时比值 1.0003 不再命中")

    print()
    print("=" * 74)
    if n_pass == n_all:
        print(f"全部通过（{n_pass} 条）。时序 → 风险分级链路口径正确：")
        print("  留出法不含待评估期；跨年优先；缺参照判 UNKNOWN；窄条不污染平台。")
    else:
        print(f"{n_pass}/{n_all} 通过，存在失败项。")
    print("=" * 74)
    return 0 if n_pass == n_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
