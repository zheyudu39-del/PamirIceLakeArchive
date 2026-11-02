"""CLI 接线验证：exposure / report / risk --dam-position 三个新入口（离线）。

这三个命令是新加的，改动全在 ``glof/cli.py``，原先没有任何脚本覆盖。
这里用真实的小样本（spike 景提取出的 4 个湖）+ 合成矢量，验证：

    1. exposure 的三态语义（居民点没给时是 None，不是 False）
    2. report 的文档结构（1 个 H1、页脚只出现一次、标题不套两层）
    3. report 里 UNKNOWN / None 的措辞（不写成 LOW / 0.00）
    4. risk --dam-position 的湖-时序面积校验（拿错 --lakes 必须报错，
       不能静默把坝体估到别的湖上）
    5. --help 里所有子命令都在

全程不联网，约 5 秒跑完。

用法：
    ./.venv/Scripts/python.exe scripts/verify_cli_wiring.py
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

PY = str(Path(sys.executable))


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def run_cli(*args: str) -> tuple[int, str, str]:
    """跑 `python -m glof.cli ...`，返回 (退出码, stdout, stderr)。"""
    proc = subprocess.run(
        [PY, "-m", "glof.cli", *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
             "PYTHONPATH": str(ROOT)},
    )
    return proc.returncode, proc.stdout, proc.stderr


def _lakes_geojson(path: Path) -> Path:
    """写一份 4 个湖的 GeoJSON（数值取自 spike 景的真实提取结果）。"""
    feats = []
    data = [
        (1, 0.0757, 77.07418, 35.73258),
        (2, 0.0418, 77.09097, 35.72353),
        (3, 0.0330, 77.23875, 35.78135),
        (4, 0.0091, 77.06955, 35.73675),
    ]
    for lid, area, lon, lat in data:
        d = 0.002
        ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
                [lon - d, lat + d], [lon - d, lat - d]]
        feats.append({
            "type": "Feature",
            "properties": {
                "lake_id": lid, "area_km2": area, "perimeter_km": 1.7,
                "mean_ndwi": 0.27, "n_pixels": 757,
                "centroid_lon": lon, "centroid_lat": lat,
                "risk_level": "UNKNOWN", "risk_reasons": [],
            },
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _roads_geojson(path: Path) -> Path:
    doc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "test-road"},
        "geometry": {"type": "LineString",
                     "coordinates": [[77.05, 35.73], [77.10, 35.73],
                                     [77.20, 35.76], [77.25, 35.78]]},
    }]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_help():
    print("\n[1] --help 里所有子命令都在")
    code, out, _ = run_cli("--help")
    check("退出码 0", code == 0, str(code))
    for name in ("info", "fetch", "latest", "inventory", "extract",
                 "risk", "accuracy", "exposure", "report"):
        check(f"有 {name} 子命令", name in out)


def test_exposure(tmp: Path):
    print("\n[2] exposure：三态语义与缓冲判定")
    lakes = _lakes_geojson(tmp / "lakes.geojson")
    roads = _roads_geojson(tmp / "roads.geojson")
    out_dir = tmp / "exp"

    code, out, err = run_cli(
        "exposure", "--lakes", str(lakes), "--roads", str(roads),
        "--buffer-km", "5", "--out", str(out_dir),
    )
    check("退出码 0", code == 0, err.strip()[:80])
    payload = json.loads((out_dir / "exposure.json").read_text(encoding="utf-8"))
    check("输出 4 条记录", len(payload) == 4, str(len(payload)))

    # 没给 --settlements：两个居民点字段必须是 null，不是 false
    check("居民点距离是 null（不是 0）",
          all(r["settlement_distance_km"] is None for r in payload))
    check("居民点缓冲判定是 null（不是 false）",
          all(r["in_settlement_buffer"] is None for r in payload))
    check("道路距离是数", all(isinstance(r["road_distance_km"], float) for r in payload))
    check("道路缓冲判定是 bool",
          all(isinstance(r["in_road_buffer"], bool) for r in payload))
    check("5 km 内都判为是", all(r["in_road_buffer"] for r in payload))
    check("终端提示说明未提供居民点",
          "未提供" in out and "不是 False" in out, out.strip()[:60])

    # 缓冲收紧到 0.1 km：只有很近的湖才算「是」
    out_dir2 = tmp / "exp2"
    run_cli("exposure", "--lakes", str(lakes), "--roads", str(roads),
            "--buffer-km", "0.1", "--out", str(out_dir2))
    p2 = json.loads((out_dir2 / "exposure.json").read_text(encoding="utf-8"))
    check("0.1 km 缓冲下有湖被排除",
          not all(r["in_road_buffer"] for r in p2),
          f"{sum(1 for r in p2 if r['in_road_buffer'])}/4 在缓冲内")

    # 漏给 buffer-km 必须失败（阈值不藏默认值）
    code3, _, err3 = run_cli("exposure", "--lakes", str(lakes), "--roads", str(roads))
    check("漏给 --buffer-km 直接报错", code3 != 0, err3.strip()[:60])

    # 道路文件不存在要明确报错，不是静默返回空
    code4, _, err4 = run_cli("exposure", "--lakes", str(lakes),
                             "--roads", str(tmp / "nope.geojson"),
                             "--buffer-km", "5")
    check("道路文件缺失时报错", code4 != 0 and "不存在" in err4, err4.strip()[:60])


def test_report(tmp: Path):
    print("\n[3] report：文档结构与诚实措辞")
    lakes = _lakes_geojson(tmp / "lakes.geojson")
    acc = tmp / "accuracy.json"
    acc.write_text(json.dumps({
        "n_truth": 28, "n_covered": 28, "n_matched": 3,
        "coverage_rate": 1.0, "point_recall": 0.1071,
        "radius_km": 0.15, "n_missing_imagery": 0,
    }, ensure_ascii=False), encoding="utf-8")
    out_dir = tmp / "rep"

    code, out, err = run_cli(
        "report", "--lakes", str(lakes), "--accuracy", str(acc),
        "--out", str(out_dir),
    )
    check("退出码 0", code == 0, err.strip()[:80])
    md = (out_dir / "report.md").read_text(encoding="utf-8")

    h1 = re.findall(r"^# ", md, re.M)
    h2 = re.findall(r"^## ", md, re.M)
    check("只有一个 H1（标题不套两层）", len(h1) == 1, f"{len(h1)} 个")
    check("有湖泊清单与精度评价两章", len(h2) == 2, str(h2))
    # 「数据来源」在顶部元信息与页脚各出现一次，这是设计如此。
    # 要守的是**页脚本身只出现一次**——拼接章节时最容易把页脚复制成两份。
    footer = md.rsplit("\n---\n", 1)[-1] if "\n---\n" in md else ""
    check("页脚只有一份", footer.count("数据来源：") == 1,
          f"页脚里 {footer.count('数据来源：')} 次")
    check("页脚带生成时间", "生成时间" in footer)
    check("没有孤立的重复生成时间",
          md.count("生成时间") == 2, f"{md.count('生成时间')} 次")

    # 诚实措辞
    check("UNKNOWN 渲染成「未判定」", "未判定" in md)
    check("不把 UNKNOWN 写成 LOW", "低风险 0" in md)
    check("精度章节带「不是边界精度」声明",
          "不是边界精度" in md and "IoU" in md)
    check("召回率有分母说明", "分母" in md)

    html = (out_dir / "report.html").read_text(encoding="utf-8")
    check("HTML 自包含（无外部引用）",
          "http://" not in html and "https://" not in html)
    check("HTML 含表格", "<table" in html)

    # 一个产物都不给必须报错
    code2, _, err2 = run_cli("report", "--out", str(tmp / "rep_empty"))
    check("无产物时报错", code2 != 0, err2.strip()[:60])


def test_risk_dam_guard(tmp: Path):
    print("\n[4] risk --dam-position：拿错 --lakes 必须报错")
    lakes = _lakes_geojson(tmp / "lakes.geojson")
    series = tmp / "series.csv"
    # 造一份面积远大于 --lakes 主湖的时序（1.82 km² vs 0.0757 km²）
    series.write_text(
        "date,area_km2,valid_px,valid_frac,tile,cloud,scene,ndwi_max,water_frac\n"
        "2018-08-06,1.8254,378324,1.0,T43SFV,1.0,x,0.8,0.5\n"
        "2019-09-20,0.2150,378324,1.0,T43SFV,1.0,x,0.5,0.1\n",
        encoding="utf-8",
    )
    code, out, err = run_cli(
        "risk", "--input", str(series), "--lake-type", "ice",
        "--dem", "https://example.invalid/dem.tif",
        "--dam-position", "--lakes", str(lakes),
    )
    check("面积对不上时报错退出", code != 0, str(code))
    check("错误信息点明差了一个量级",
          "量级" in err, err.strip().splitlines()[0][:70] if err.strip() else "")
    check("错误信息提示 --lakes 可能拿错景",
          "--lakes" in err and ("另一景" in err or "另一个 bbox" in err))

    # --dam-position 不给 --dem 要报错
    code2, _, err2 = run_cli("risk", "--input", str(series), "--lake-type", "ice",
                             "--dam-position", "--lakes", str(lakes))
    check("缺 --dem 时报错", code2 != 0 and "--dem" in err2, err2.strip()[:60])

    # --dam-position 不给 --lakes 要报错
    code3, _, err3 = run_cli("risk", "--input", str(series), "--lake-type", "ice",
                             "--dem", "https://example.invalid/dem.tif",
                             "--dam-position")
    check("缺 --lakes 时报错", code3 != 0 and "--lakes" in err3, err3.strip()[:60])


def test_core_no_qt():
    print("\n[5] 分层约束")
    bad = []
    for path in (ROOT / "glof" / "core").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import|from)\s+(PySide6|PyQt)", text, re.M):
            bad.append(path.name)
    check("glof/core/ 没有 import Qt", not bad, str(bad))


def main() -> int:
    print("=" * 70)
    print("CLI 接线验证（离线）")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_help()
        test_exposure(tmp)
        test_report(tmp)
        test_risk_dam_guard(tmp)
        test_core_no_qt()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"失败 {len(FAILURES)} / {CHECKS[0]}：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"全部通过：{CHECKS[0]} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
