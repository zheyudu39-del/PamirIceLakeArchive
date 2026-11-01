"""验证事件年查询：lake_profile 从 ICIMOD 库把"点位 → 坝型/事件年"查出来。

这是界面风险接线的第一段：选中湖的质心要能查到它的已知溃决年份，
替代 app_state 里硬编码的 EVENT_YEARS=(2018,)。检验口径：

* 克亚吉尔 (35.677371, 77.190788)（与 scripts/kyagar_series.py 同一点位）
  在半径 0.05° 内应命中 34 条事件、聚成一个点，坝型为冰坝系，
  事件年含 2018（docs §7.9 的溃决事件）；
* 库文件缺失/不存在路径 → 返回 None 而不抛（界面据此如实显示"无记录"，
  这是 inventory.lake_profile 的诚实性约定）；
* 离库很远的点 → None（不是每个湖都有溃决记录，不能编）。

运行：
    .venv\\Scripts\\python.exe scripts\\verify_event_years.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glof.core import risk as grisk  # noqa: E402
from glof.core.inventory import DEFAULT_DB_PATH, lake_profile  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

KYAGAR_LAT, KYAGAR_LON = 35.677371, 77.190788

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
    print("步骤 1 / 克亚吉尔点位画像（默认库，半径 0.05°）")
    print("=" * 74)
    check("默认库文件在位", DEFAULT_DB_PATH.is_file(), str(DEFAULT_DB_PATH))
    profile = lake_profile(KYAGAR_LAT, KYAGAR_LON)
    check("profile 非 None（点位命中已知溃决湖）", profile is not None)
    if profile is None:
        print(f"{n_pass}/{n_all} 通过，存在失败项。")
        return 1
    check("名字含 Kyagar（大小写不敏感）", "kyagar" in profile.name.lower(),
          f"name={profile.name!r}")
    check("坝型属冰坝系（库值含 ice）", "ice" in profile.lake_type.lower(),
          f"lake_type={profile.lake_type!r}")
    check("库坝型值经下划线归一后能被 risk 别名表识别为冰坝",
          grisk.lake_type_kind(profile.lake_type.lower().replace(" ", "_")) == "ice",
          f"kind={grisk.lake_type_kind(profile.lake_type.lower().replace(' ', '_'))}")
    check("事件数 ≥ 30（实测 34 条记录）", profile.n_events >= 30,
          f"n_events={profile.n_events}")
    check("2018 ∈ event_years（docs §7.9 溃决事件年）", 2018 in profile.event_years,
          f"{profile.first_year}–{profile.last_year} 共 {len(profile.event_years)} 个事件年")
    check("事件年升序且无重复", profile.event_years == sorted(set(profile.event_years)))
    check("事件年范围与库一致（1880–2019，kyagar_series.py 记载）",
          profile.first_year == 1880 and profile.last_year == 2019,
          f"{profile.first_year}–{profile.last_year}")
    check("聚类质心落在查询点半径内",
          abs(profile.lat - KYAGAR_LAT) <= 0.05 and abs(profile.lon - KYAGAR_LON) <= 0.05,
          f"({profile.lat:.6f}, {profile.lon:.6f})")

    print()
    print("=" * 74)
    print("步骤 2 / 诚实性：库缺失不抛、远点如实 None")
    print("=" * 74)
    missing = ROOT / "data" / "raw" / "__no_such_glof_db__.zip"
    try:
        gone = lake_profile(KYAGAR_LAT, KYAGAR_LON, db=missing)
        check("库指向不存在路径 → None 且不抛", gone is None, f"返回 {gone!r}")
    except Exception as exc:  # noqa: BLE001
        check("库指向不存在路径 → None 且不抛", False, f"抛了 {type(exc).__name__}: {exc}")
    far = lake_profile(39.9042, 116.4074)  # 北京：库覆盖 HMA，不应有点
    check("远离研究区的点 → None（没有记录不编造）", far is None,
          f"返回 {far!r}")

    print()
    print("=" * 74)
    if n_pass == n_all:
        print(f"全部通过（{n_pass} 条）。事件年查询就位：")
        print("  点位 → 库画像（坝型/事件数/事件年）可替代 EVENT_YEARS 硬编码；")
        print("  库缺失返回 None，界面按'无记录'处理。")
    else:
        print(f"{n_pass}/{n_all} 通过，存在失败项。")
    print("=" * 74)
    return 0 if n_pass == n_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
