"""报告导出模块验证：表格转义 -> 缺失值措辞 -> HTML 自包含 -> 空数据 -> 真实产物。

这个模块最容易出的错不是崩溃，是**把缺失渲染成一个正常值**：

    * UNKNOWN 等级印成「低风险」—— 把"不知道"说成"安全"；
    * 面积 None 印成 0.00 —— 把"没量到"说成"湖干了"；
    * 表格里的 ``|`` 没转义 —— 一行裂成两行，后面所有列错位；
    * HTML 引了外部字体/CDN —— 答辩现场断网，报告样式全丢。

所以每条断言都盯着这四件事，而不是"函数能跑通"。第 6 节用仓库里真实跑出来的
``output/risk/risk_assessments.json``（克亚吉尔时序，存在才跑）做一次端到端渲染，
确认真实产物里的 UNKNOWN 行也走同一条措辞。

用法：
    ./.venv/Scripts/python.exe scripts/verify_report.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glof.core import report as grep  # noqa: E402
from glof.core.accuracy import EvaluationReport, PointMatch  # noqa: E402
from glof.core.risk import RiskLevel, assess_ice_dammed, assess_moraine_dammed  # noqa: E402
from glof.core.water import LakeRecord  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

RISK_JSON = ROOT / "output" / "risk" / "risk_assessments.json"


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def expect_value_error(name: str, fn) -> None:
    try:
        fn()
    except ValueError as exc:
        check(name, True, f"ValueError: {exc}")
    else:
        check(name, False, "没有抛 ValueError")


def make_record(
    lake_id: int,
    area_km2: float | None,
    lon: float = 77.19,
    lat: float = 35.68,
    *,
    risk_level: str = "UNKNOWN",
    risk_reasons: list[str] | None = None,
) -> LakeRecord:
    """造一条湖记录。area_km2=None 表示没量到面积（不是 0）。

    几何用正方形环，和 scripts/smoke_ui_latest.py 的 make_record 同口径——
    报告不读几何，但保持构造方式一致，免得将来加了"多边形面积核对"时两处对不上。
    """
    d = 0.004
    ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
            [lon - d, lat + d], [lon - d, lat - d]]
    area_m2 = None if area_km2 is None else area_km2 * 1e6
    return LakeRecord(
        lake_id=lake_id,
        area_m2=area_m2,  # type: ignore[arg-type]
        perimeter_m=1000.0,
        n_pixels=100,
        area_px_m2=0.0 if area_m2 is None else area_m2,
        mean_ndwi=0.4,
        centroid_lon=lon,
        centroid_lat=lat,
        geometry_wgs84_json=json.dumps({"type": "Polygon", "coordinates": [ring]}),
        risk_level=risk_level,
        risk_reasons=list(risk_reasons or []),
    )


def split_md_row(line: str) -> list[str]:
    """独立实现的 Markdown 行切分（不调 report._split_row，避免用被测代码验被测代码）。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            buf.append(text[i + 1])
            i += 2
            continue
        if text[i] == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(text[i])
        i += 1
    cells.append("".join(buf).strip())
    return cells


def table_rows(markdown_text: str) -> list[list[str]]:
    """取出 Markdown 里第一张表：返回 [表头行, 数据行...]（不含 ``| --- |`` 分隔行）。"""
    lines = markdown_text.splitlines()
    rows: list[list[str]] = []
    header_at: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|"):
            if header_at is None:
                if i + 1 < len(lines):
                    sep = lines[i + 1].strip().replace("|", "").replace(" ", "")
                    if sep and set(sep) <= set(":-") and "-" in sep:
                        header_at = i
                        rows.append(split_md_row(stripped))
                continue
            # 跳过表头之后的分隔行
            if i == header_at + 1:
                continue
            rows.append(split_md_row(stripped))
        elif header_at is not None:
            break
    return rows


def first_data_row(markdown_text: str) -> list[str]:
    rows = table_rows(markdown_text)
    return rows[1] if len(rows) > 1 else []


# --------------------------------------------------------------------------
def test_table_escaping():
    """Markdown 表格：列数正确，单元格里的 | 不裂列。"""
    print("\n[1] Markdown 表格生成与转义")
    md = grep.build_markdown(
        title="表格测试",
        sections=[
            grep.ReportSection(
                heading="样例",
                table=grep.ReportTable(
                    headers=["编号", "说明", "值"],
                    rows=[
                        [1, "含竖线 a|b 的说明", 0.5],
                        [2, "普通文本", None],
                    ],
                ),
            )
        ],
    )
    rows = table_rows(md)
    check("表格有表头 + 2 行数据", len(rows) == 3, f"{len(rows)} 行")
    check("表头 3 列", len(rows[0]) == 3, str(rows[0]))
    check("每行列数与表头一致", all(len(r) == 3 for r in rows),
          " / ".join(f"{len(r)}" for r in rows))
    check("竖线被转义成 \\|", "a\\|b" in md, next((l for l in md.splitlines() if "含竖线" in l), ""))
    check("转义后单元格内容可还原", rows[1][1] == "含竖线 a|b 的说明", rows[1][1])
    check("第三列仍是 0.5（没有因竖线错位）", rows[1][2] == "0.5000", rows[1][2])
    check("None 单元格渲染成「没量到」", rows[2][2] == "没量到", rows[2][2])

    # 列数不匹配必须报错，不能悄悄生成错位的表
    expect_value_error(
        "数据行列数少于表头时报 ValueError",
        lambda: grep.build_markdown(
            title="坏表",
            sections=[grep.ReportSection(heading="x", table=grep.ReportTable(["a", "b"], [[1]]))],
        ),
    )
    # 二维序列形式（第一行当表头）也要能用
    md2 = grep.build_markdown(
        title="序列形式",
        sections=[grep.ReportSection(heading="x", table=[["a", "b"], [1, 2]])],
    )
    check("二维序列按首行作表头", len(table_rows(md2)) == 2 and table_rows(md2)[0] == ["a", "b"])


def test_unknown_level():
    """UNKNOWN 必须渲染成「未判定（缺数据：…）」，绝不能是「低风险」。"""
    print("\n[2] UNKNOWN 不渲染成 LOW")
    reason = ("[缺数据未评估] 坝体下游坡度：未接入 DEM，"
              "无法量测坝体下游坡度（需 30 m DEM + 坝体位置）")
    record = make_record(1, 1.2, risk_level="UNKNOWN", risk_reasons=[reason])
    md = grep.build_lake_report([record], title="风险措辞")

    row = first_data_row(md)
    check("表格有数据行", len(row) == 5, str(row))
    level_cell = row[3]
    check("等级列以「未判定」开头", level_cell.startswith("未判定"), level_cell)
    check("等级列带「缺数据：」并写明原因",
          "缺数据：" in level_cell and "未接入 DEM" in level_cell, level_cell)
    check("等级列不是「低风险」", "低风险" not in level_cell, level_cell)
    check("判据摘要列保留原因", "未接入 DEM" in row[4], row[4])

    # 对照：真的 LOW 仍渲染成低风险，说明不是把所有等级都改写了
    low = make_record(2, 0.5, risk_level="LOW",
                      risk_reasons=["[未命中] 面积/平台比值 = 0.27（阈值 0.95）"])
    md_low = grep.build_lake_report([low], title="对照")
    check("LOW 渲染成「低风险」", first_data_row(md_low)[3] == "低风险",
          first_data_row(md_low)[3])

    # 逐期判定报告走同一套措辞（risk.assess_moraine_dammed 缺 DEM/RGI 必为 UNKNOWN）
    # 基准面积与当前面积相同 → 扩张速率 0，未命中；坡度与母冰川都没数据 → 两条 UNKNOWN。
    # 一条没中但仍有缺口，按 _decide_level 必须是 UNKNOWN 而不是 LOW。
    assessment = assess_moraine_dammed(0.8, baseline_area_km2=0.8, baseline_year=2015.0,
                                       year=2020.0)
    check("构造的冰碛坝判定确实是 UNKNOWN", assessment.level is RiskLevel.UNKNOWN,
          assessment.level.value)
    md_risk = grep.build_risk_report([assessment], title="逐期")
    risk_level_cell = first_data_row(md_risk)[3]
    check("逐期表的 UNKNOWN 也是「未判定（缺数据：…）」",
          risk_level_cell.startswith("未判定") and "缺数据：" in risk_level_cell,
          risk_level_cell)
    check("逐期表未把 UNKNOWN 写成低风险", "低风险" not in risk_level_cell, risk_level_cell)


def test_missing_area():
    """面积为 None / NaN 必须写「没量到」，不能写 0.00。"""
    print("\n[3] 面积缺失不写 0.00")
    md = grep.build_lake_report(
        [make_record(1, None), make_record(2, 1.5, lon=77.2, lat=35.7)],
        title="面积缺失",
    )
    rows = table_rows(md)
    missing_row = rows[1]
    check("面积列是「没量到」", missing_row[1] == "没量到", missing_row[1])
    check("面积列不含「0.00」", "0.00" not in missing_row[1], missing_row[1])
    check("质心列正常渲染", "35.6800" in missing_row[2] and "77.1900" in missing_row[2],
          missing_row[2])
    check("合计只统计量到的湖，并说明另有几个没量到",
          "1.5000 km²" in md and "另有 1 个湖" in md and "未计入合计" in md,
          next((l for l in md.splitlines() if "合计面积" in l), "（无合计行）"))
    check("合计行不把缺失湖当成 0", "1.5000 km²（仅统计" in md, "")

    # NaN 与 None 同样处理：NaN 是"算出来不是数"，不是 0
    md_nan = grep.build_lake_report([make_record(3, float("nan"))], title="NaN")
    check("NaN 面积也渲染成「没量到」", table_rows(md_nan)[1][1] == "没量到",
          table_rows(md_nan)[1][1])

    # 极小但非零的面积不能被四舍五入成 0.0000
    md_tiny = grep.build_lake_report([make_record(4, 0.00004)], title="极小")
    tiny_cell = table_rows(md_tiny)[1][1]
    check("4e-5 km² 不印成 0.0000 且仍可读出非零值",
          tiny_cell != "0.0000" and float(tiny_cell) > 0, tiny_cell)

    # 质心缺失同样不能补 0°
    md_nocoord = grep.build_lake_report(
        [make_record(5, 1.0, lon=float("nan"), lat=float("nan"))], title="质心缺失")
    check("质心缺失渲染成「没量到」而不是 0°",
          table_rows(md_nocoord)[1][2] == "没量到", table_rows(md_nocoord)[1][2])


def test_html_self_contained():
    """HTML 自包含：有 <table> 与内联 <style>，没有任何外部引用。"""
    print("\n[4] to_html 自包含")
    md = grep.build_lake_report(
        [make_record(1, 1.2, risk_level="HIGH",
                     risk_reasons=["[命中] 面积/平台比值 = 1.0003（阈值 0.95）"])],
        title="HTML 自包含",
        meta={"数据来源": "ICIMOD HMA GLOF v4.0", "口径说明": "留出法估平台"},
    )
    page = grep.to_html(md, title="HTML 自包含")

    check("含 <table>", "<table>" in page)
    check("含 <thead> / <tbody>", "<thead>" in page and "<tbody>" in page)
    check("含内联 <style>", "<style>" in page and "</style>" in page)
    check("样式不是空壳（有 table 规则）", "table {" in page or "table{" in page)
    check("没有 <link> 标签", "<link" not in page.lower())
    check("没有 <script> 标签", "<script" not in page.lower())
    check("没有 <img> 标签", "<img" not in page.lower())
    check("没有 @import", "@import" not in page.lower())
    check("不含 http://", "http://" not in page.lower())
    check("不含 https://", "https://" not in page.lower())
    check("含 lang=\"zh-CN\"", 'lang="zh-CN"' in page)
    check("含 <title>", "<title>HTML 自包含</title>" in page, "")
    check("正文内容进了 HTML（数据来源）", "ICIMOD HMA GLOF v4.0" in page)
    check("表格数据进了 HTML", "高风险" in page and "1.2000" in page)

    # 孤立的 | 行（不成表）不能把解析器卡死：早先缺兜底分支时这里会死循环
    orphan = grep.to_html("# 标题\n\n| 这不是表\n\n正文", title="孤立竖线")
    check("不成表的 | 行不死循环、被当正文渲染",
          "这不是表" in orphan and "<p>" in orphan)

    # 即便 Markdown 里带 URL，也只作为纯文本出现
    md_url = grep.build_markdown(
        title="带链接",
        sections=[grep.ReportSection(heading="来源", body="见 https://example.org/data 的说明")],
    )
    page_url = grep.to_html(md_url, title="带链接")
    check("正文 URL 保持纯文本，不生成 <a href>",
          "https://example.org/data" in page_url and "<a " not in page_url.lower())


def test_html_escaping():
    """特殊字符与中文在 HTML 里被正确转义，不破坏结构。"""
    print("\n[5] HTML 转义")
    evil_body = "正文含 <script>alert(1)</script> 与 a & b，以及 **粗体<tag>**"
    md = grep.build_markdown(
        title="转义 <测试> & 中文",
        sections=[
            grep.ReportSection(
                heading="小节 <h2> & 标题",
                body=evil_body,
                table=grep.ReportTable(
                    headers=["列 <1>", "列 & 2"],
                    rows=[["<b>x</b>", "a & b | c"]],
                ),
            )
        ],
        meta={"数据来源": "源 <A> & B"},
    )
    page = grep.to_html(md, title="转义 <测试> & 中文")

    check("正文 <script> 被转义", "<script>alert" not in page and "&lt;script&gt;" in page)
    check("正文 & 被转义成 &amp;", "a &amp; b" in page)
    check("标题里的 < > & 被转义",
          "转义 &lt;测试&gt; &amp; 中文" in page, "")
    check("小节标题里的尖括号被转义", "小节 &lt;h2&gt; &amp; 标题" in page)
    check("表头里的尖括号被转义", "列 &lt;1&gt;" in page)
    check("单元格里的 <b> 被转义", "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page)
    check("单元格里的 & 被转义", "a &amp; b | c" in page)
    check("粗体标签正常生成", "<strong>" in page and "</strong>" in page)
    check("中文原样保留", "中文" in page)
    check("HTML 结构未被破坏（闭合标签齐全）",
          page.count("<table>") == page.count("</table>")
          and page.count("<main") == page.count("</main>"))


def test_empty():
    """空记录列表不崩，输出「无数据」而不是空表。"""
    print("\n[6] 空数据")
    md = grep.build_lake_report([], title="空清单")
    check("含「无数据」", "无数据" in md)
    check("不输出空表格（没有表头行）", "| 编号 |" not in md)
    check("没有崩，返回了标题", md.startswith("# 空清单"))

    md_risk = grep.build_risk_report([], title="空判定")
    check("逐期判定空输入也输出「无数据」", "无数据" in md_risk and "| 日期 |" not in md_risk)

    page = grep.to_html(md, title="空清单")
    check("空报告也能转 HTML", "<main" in page and "无数据" in page)

    md_sections = grep.build_markdown(title="零章节", sections=[])
    check("零章节不崩且仍有页脚", "数据来源" in md_sections and "生成时间" in md_sections)

    expect_value_error("空标题报 ValueError", lambda: grep.build_markdown(title="  ", sections=[]))


def test_footer_and_write():
    """页脚必须有数据来源与生成时间；write_markdown 落 UTF-8。"""
    print("\n[7] 页脚与落盘")
    md = grep.build_markdown(
        title="页脚",
        sections=[grep.ReportSection(heading="x", body="正文")],
        meta={"数据来源": "Sentinel-2 L2A / Planetary Computer", "口径说明": "NDWI > 0.15"},
    )
    check("页脚含「数据来源：」", "数据来源：Sentinel-2 L2A / Planetary Computer" in md)
    check("页脚含「生成时间：」", "生成时间：" in md)
    check("元信息块也写了口径说明", "口径说明：NDWI > 0.15" in md)
    check("页脚出现在正文之后", md.rfind("数据来源：") > md.rfind("## x"))

    md_no_meta = grep.build_markdown(title="无来源", sections=[])
    check("没给数据来源时明说「未注明」，不编一个",
          "数据来源：未注明" in md_no_meta,
          next((l for l in md_no_meta.splitlines() if l.startswith("数据来源")), "（无）"))

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "report.md"
        written = grep.write_markdown(target, md)
        check("write_markdown 返回 Path", isinstance(written, Path), type(written).__name__)
        check("父目录被自动创建", target.exists())
        raw = target.read_bytes()
        check("落盘为 UTF-8（中文可解码）",
              raw.decode("utf-8") == md, f"{len(raw)} 字节")
        check("无 BOM", not raw.startswith(b"\xef\xbb\xbf"))


def test_accuracy_report():
    """精度章节必须带「点位召回不是边界精度」的声明。"""
    print("\n[8] 精度评价章节")
    matches = [
        PointMatch("site001", 36.34, 75.47, 1.3224, False, 2, 0.0016),
        PointMatch("site004", 36.62, 74.08, 0.0, True, 1, 0.0436),
        PointMatch("site008", 35.93, 74.60, None, False, None, None),
    ]
    rep = EvaluationReport(
        n_truth=55, n_covered=28, n_matched=3,
        coverage_rate=28 / 55, point_recall=3 / 28, radius_km=0.15,
        matches=matches,
        notes=["point_recall 分母是被影像覆盖的点位数，不含无影像点位",
               "未做边界精度评价（无人工标注的面真值）"],
    )
    md = grep.build_accuracy_report(rep)
    check("含召回率百分比", "10.7%" in md, "")
    check("写明分母是覆盖数而不是真值总数",
          "被影像覆盖的 28 个点位" in md and "不是真值总数 55" in md)
    check("含「点位召回不是边界精度」声明", "点位召回不是边界精度" in md)
    check("声明里点明不能写成 IoU/F1",
          "不能写成 IoU 或 F1" in md, "")
    check("含口径 notes", "无人工标注的面真值" in md)
    check("逐点位表里 distance=None 写「没量到」", "| site008 | 没量到 |" in md, "")
    check("逐点位表说明没量到不是距离 0", "不是距离为 0" in md)
    check("没有 pixel_counts 时不编像元级指标", "像元级指标" not in md)

    empty = EvaluationReport(
        n_truth=0, n_covered=0, n_matched=0,
        coverage_rate=0.0, point_recall=0.0, radius_km=0.15, matches=[],
        notes=["没有真值点位，无可评价对象"],
    )
    md_empty = grep.build_accuracy_report(empty)
    empty_cells = [cell for row in table_rows(md_empty) for cell in row]
    check("分母为零时表格里不出现 0.0%",
          not any(cell.endswith("%") and cell.startswith("0.0") for cell in empty_cells),
          " / ".join(empty_cells))
    check("分母为零时写「没量到（无真值点位）」", "没量到（无真值点位）" in md_empty)


def test_real_artifacts():
    """真实产物端到端：仓库里的 risk_assessments.json 能渲染成一份自包含 HTML。"""
    print("\n[9] 真实产物（output/risk/risk_assessments.json）")
    if not RISK_JSON.exists():
        print("      （没有 output/risk/risk_assessments.json，跳过——不算失败）")
        return
    payload = json.loads(RISK_JSON.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    check("读到真实判定行", len(rows) > 0, f"{len(rows)} 期")
    md = grep.build_risk_report(
        rows,
        title="克亚吉尔逐期风险判定",
        meta={
            "数据来源": str(payload.get("source", "未注明")),
            "坝型": payload.get("lake_type", "未注明"),
            "阈值": payload.get("thresholds", {}),
        },
    )
    table = table_rows(md)
    check("表格行数 = 判定行数 + 表头", len(table) == len(rows) + 1,
          f"{len(table)} vs {len(rows) + 1}")
    check("真实数据里至少有一期判 LOW（LOW 正常渲染）", "低风险" in md)
    check("真实数据的参照政策进了表", "留出" in md or "基准期" in md,
          next((l for l in md.splitlines() if "留出" in l or "基准期" in l), "（无）"))
    page = grep.to_html(md, title="克亚吉尔逐期风险判定")
    check("真实报告能转成自包含 HTML", "<table>" in page and "<style>" in page)
    check("真实报告 HTML 无外部引用",
          "http://" not in page.lower() and "https://" not in page.lower())


def main() -> int:
    print("=" * 70)
    print("报告导出模块验证")
    print("=" * 70)

    test_table_escaping()
    test_unknown_level()
    test_missing_area()
    test_html_self_contained()
    test_html_escaping()
    test_empty()
    test_footer_and_write()
    test_accuracy_report()
    test_real_artifacts()

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
