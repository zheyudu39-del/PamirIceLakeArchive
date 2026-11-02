"""报告导出：把跑出来的结果整理成人能读、也能逐条复核的 Markdown 与 HTML。

为什么自己写，而不是拉一个 Markdown 库
--------------------------------------
报告要能跟着仓库走、在答辩现场任何一台机器上打开，所以**不新增依赖**：
Markdown 由本模块直接拼字符串，HTML 只用到标准库的 ``html.escape`` 与正则。
``requirements.txt`` 里原计划给报告阶段留的 ``python-docx`` / ``jinja2`` 至今没放开，
这个模块让它们继续不用放开。

为什么报告里到处都是「没量到」和「未判定」
------------------------------------------
本项目最容易出的错不是崩溃，是**悄悄给出一个好看的数**。已有的实测例子：

* ``output/eval/accuracy.json`` 当前是 28 个窗口、命中 3、point_recall 10.71%
  （半径 0.15 km）。如果只写「召回率 10.7%」而不写分母是"被影像覆盖的 28 个点位"、
  不写"ICIMOD 是历史溃决事件库、当前残湖可能已排空"，这个数会被读成算法很差；
* ``risk.py`` 在缺 DEM / 缺 RGI 时判 UNKNOWN。报告若把 UNKNOWN 印成「低风险」，
  等于把"不知道"说成"安全"——这是这类系统最危险的失败方式；
* 面积为 ``None`` 时写成 ``0.00``，下游会当成"湖干了"。

所以本模块的渲染规则只有一条：**缺失值必须保留为缺失的措辞**。
``None`` / ``NaN`` 一律渲染成「没量到」，``UNKNOWN`` 一律渲染成
「未判定（缺数据：<原因>）」，并且报告页脚固定带数据来源与生成时间。

HTML 是自包含的
---------------
``to_html()`` 只认一个很小的 Markdown 子集：``#`` 标题、段落、``**粗体**``、
``` `代码` ```、``-`` 列表、``| a | b |`` 表格、行内换行、``>`` 引用块与 ``---``。
样式全部内联在 ``<style>`` 里，字体只用系统字体栈，不引 CDN、不写 ``@import``、
不生成 ``<a href>`` / ``<img src>``——生成出来的单个 ``.html`` 文件断网也能看。
正文里的 URL 会原样作为**纯文本**保留，不会变成可点击的外部引用。
"""

from __future__ import annotations

import html as _html
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:  # 只用于标注：本模块运行时只依赖标准库
    from .accuracy import EvaluationReport
    from .risk import RiskAssessment
    from .water import LakeRecord

__all__ = [
    "ReportTable",
    "ReportSection",
    "build_markdown",
    "write_markdown",
    "to_html",
    "build_lake_report",
    "build_risk_report",
    "build_accuracy_report",
]

# 缺数据时统一用这三个字，与 latest.py / CLI 的措辞一致。
MISSING_TEXT = "没量到"

# 风险等级的中文措辞。UNKNOWN 不在这里——它必须带原因，单独处理。
_LEVEL_LABELS = {"HIGH": "高风险", "MEDIUM": "中风险", "LOW": "低风险"}

# 判据 describe() 的前缀，如 "[缺数据未评估] 坝体下游坡度：..."。
_TAG_RE = re.compile(r"^\[[^\]]*\]\s*")

# 生成时间。带时区，避免"几点"在异地答辩时说不清。
def _now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ReportTable:
    """一张表：表头 + 数据行。

    单元格可以是任意值，渲染时统一走 ``_cell_text()``——``None`` / ``NaN``
    会变成「没量到」，而不是空字符串或 0。列数不匹配直接 ``ValueError``：
    悄悄少一列的表，在报告里没人看得出来，但每个数都会错位。
    """

    headers: Sequence[Any]
    rows: Sequence[Sequence[Any]] = field(default_factory=list)


@dataclass
class ReportSection:
    """报告的一章。

    ``body`` 是 Markdown 正文；``table`` 可以是 ``ReportTable``，也可以是
    "第一行当表头"的二维序列（``[["a","b"], [1,2]]``），方便直接贴数据。
    """

    heading: str
    body: str = ""
    table: ReportTable | Sequence[Sequence[Any]] | None = None


# ---------------------------------------------------------------------------
# 取值与格式化（全部围绕"缺失不冒充数字"）
# ---------------------------------------------------------------------------


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时支持对象属性与 Mapping 取值，报告层不该要求调用方先转成一种形状。

    属性取值要吞掉 ``AttributeError`` 与 ``TypeError``：``LakeRecord.area_km2``
    是 property，``area_m2`` 为 None 时它会抛 ``TypeError``。报告遇到"没量到"
    必须渲染成缺失措辞，而不是让整份报告崩掉——但默认值仍是 ``None``，
    绝不退化成 0。
    """
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    try:
        return getattr(obj, name, default)
    except (AttributeError, TypeError):
        return default


def _as_float(value: Any) -> float | None:
    """能转成有限浮点就返回，否则 None。字符串 "nan" / "" 也算没量到。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_number(value: float) -> str:
    """表格里的通用数字：固定 4 位小数。

    非零但绝对值小于 5e-5 时改用科学计数法。否则 0.00004 会被印成
    ``0.0000``——一个真实量到的值变成了 0，正是本项目最要避免的失败方式。
    """
    if not math.isfinite(value):
        return MISSING_TEXT
    if value == 0.0:
        return "0.0000"
    if abs(value) < 5e-5:
        return f"{value:.3e}"
    return f"{value:.4f}"


def _fmt_area_km2(value: float | None) -> str:
    """面积列。None 是"没量到"，不是 0；极小但非零的面积不能印成 0.0000。"""
    if value is None or not math.isfinite(value):
        return MISSING_TEXT
    if value == 0.0:
        return "0.0000"
    if abs(value) < 1e-4:
        return f"{value:.6f}"
    return f"{value:.4f}"


def _fmt_coord(lat: Any, lon: Any) -> str:
    """质心经纬度。任一缺失就整格写「没量到」，不拿 0° 顶替。"""
    lat_f, lon_f = _as_float(lat), _as_float(lon)
    if lat_f is None or lon_f is None:
        return MISSING_TEXT
    ns = "N" if lat_f >= 0 else "S"
    ew = "E" if lon_f >= 0 else "W"
    return f"{abs(lat_f):.4f}°{ns}, {abs(lon_f):.4f}°{ew}"


def _cell_text(value: Any) -> str:
    """任意单元格 → 一行纯文本（Markdown 转义前）。"""
    if value is None:
        return MISSING_TEXT
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return _fmt_number(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Mapping):
        # 值也要走 _cell_text：直接 f"{v}" 会把 None 印成字面量 "None"，
        # 那正是本模块要消灭的东西。列表/元组分支早就这么做了，这里保持一致。
        return "；".join(f"{k}={_cell_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return "、".join(_cell_text(v) for v in value)
    text = str(getattr(value, "value", value))
    return " ".join(text.split())


def _md_cell(value: Any) -> str:
    """Markdown 表格单元格：转义 ``|``、换行压成空格。

    先转义反斜杠，再转义竖线，这样调用方即使自己写了 ``a\\|b`` 也不会被
    二次解释成两列。
    """
    text = _cell_text(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _coerce_table(table: ReportTable | Sequence[Sequence[Any]]) -> ReportTable:
    """把"第一行当表头"的二维序列也接受进来。"""
    if isinstance(table, ReportTable):
        return table
    rows = [list(row) for row in table]
    if not rows:
        return ReportTable(headers=[])
    return ReportTable(headers=rows[0], rows=rows[1:])


def _render_table(table: ReportTable | Sequence[Sequence[Any]]) -> str:
    """Markdown 表格。空表输出「无数据」，不输出一条光秃秃的分隔线。

    有数据行却没有表头时直接 ``ValueError``：早先这里返回「表格为空」，
    于是 ``ReportTable([], [[1, 2]])`` 里的两行数据被悄悄吞掉，报告显示
    "无数据"——数据在内存里、却从报告里消失，比排版难看严重得多。
    """
    table = _coerce_table(table)
    headers = list(table.headers)
    if not headers:
        if table.rows:
            raise ValueError(
                f"表格有 {len(table.rows)} 行数据但没有表头；"
                "拒绝把数据悄悄渲染成「无数据」"
            )
        return "**无数据**（表格为空）"

    n = len(headers)
    lines = [
        "| " + " | ".join(_md_cell(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in range(n)) + " |",
    ]
    for index, row in enumerate(table.rows, start=1):
        cells = list(row)
        if len(cells) != n:
            raise ValueError(
                f"表格第 {index} 行有 {len(cells)} 列，表头是 {n} 列；"
                "拒绝生成会错位的表"
            )
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 风险等级与判据摘要
# ---------------------------------------------------------------------------


def _level_value(level: Any) -> str:
    """RiskLevel 枚举、字符串、None 统一成大写字符串。"""
    if level is None:
        return "UNKNOWN"
    value = getattr(level, "value", level)
    return str(value).strip().upper()


def _clean_reason(text: Any) -> str:
    """去掉 describe() 的 ``[缺数据未评估]`` 前缀，保留原因本身。"""
    return _TAG_RE.sub("", " ".join(str(text).split()))


def _unknown_cause(reasons: Sequence[Any], summary: str = "") -> str:
    """从判据文本里摘出"为什么没判定"。

    优先找带「缺数据 / 未评估 / 不足 / 无法」的判据；都没有时退回 summary；
    再没有就明说「原因未记录」——不编一个原因。
    """
    cleaned = [_clean_reason(r) for r in reasons]
    hints = [c for c in cleaned if any(k in c for k in ("缺数据", "未评估", "不足", "无法", "未提供", "无可用"))]
    picked = hints or cleaned
    if picked:
        return "；".join(picked)
    if summary:
        return _clean_reason(summary)
    return "原因未记录"


def _risk_level_text(level: Any, reasons: Sequence[Any], *, summary: str = "") -> str:
    """等级列的人读文本。

    UNKNOWN 必须写成「未判定（缺数据：…）」。这是本模块最重要的一行：
    把 UNKNOWN 写成 LOW，等于把"不知道"说成"安全"。
    """
    raw = _level_value(level)
    if raw in _LEVEL_LABELS:
        return _LEVEL_LABELS[raw]
    if raw == "UNKNOWN":
        return f"未判定（缺数据：{_unknown_cause(reasons, summary)}）"
    return f"未判定（等级无法识别：{raw!r}）"


def _reasons_text(reasons: Sequence[Any], summary: str = "") -> str:
    """判据摘要列：逐条判据用「；」连起来。没有判据时退回 summary。"""
    cleaned = [_clean_reason(r) for r in reasons]
    if cleaned:
        return "；".join(cleaned)
    if summary:
        return _clean_reason(summary)
    return "没量到（无判据记录）"


def _as_reasons(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    try:
        return list(value)
    except TypeError:
        return [value]


# ---------------------------------------------------------------------------
# Markdown 组装
# ---------------------------------------------------------------------------


def _meta_text(value: Any) -> str:
    if value is None:
        return "未注明"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return _fmt_number(value)
    if isinstance(value, (list, tuple)):
        return "、".join(_meta_text(v) for v in value)
    if isinstance(value, Mapping):
        return "；".join(f"{k}={_meta_text(v)}" for k, v in value.items())
    return str(value)


def _data_source(meta: Mapping[str, Any]) -> str:
    """页脚的数据来源。没给就明说没给，不拿项目名冒充数据出处。"""
    for key in ("数据来源", "data_source", "source"):
        if key in meta and str(meta[key]).strip():
            return _meta_text(meta[key])
    return "未注明（调用方未提供 meta 中的数据来源）"


def build_markdown(
    *,
    title: str,
    sections: Sequence[ReportSection],
    meta: Mapping[str, Any] | None = None,
) -> str:
    """把章节拼成一份完整的 Markdown 报告。

    结构固定为：标题 → 元信息块 → 各章 → 页脚（数据来源 + 生成时间）。
    页脚是硬性的：没有数据来源的报告无法复核，没有生成时间的报告无法判断新旧。

    Parameters
    ----------
    title
        一级标题。空标题直接 ``ValueError``——报告不能没有名字。
    sections
        ``ReportSection`` 列表。正文原样保留（调用方写的 Markdown），
        表格由 ``_render_table()`` 统一转义。
    meta
        生成时间以外的元信息，如 ``{"数据来源": ..., "口径说明": ...}``。
        按插入顺序输出，不排序。
    """
    if not str(title or "").strip():
        raise ValueError("报告标题不能为空")

    meta = dict(meta or {})
    generated_at = _now_text()

    lines: list[str] = [f"# {title.strip()}", ""]

    meta_lines = [f"生成时间：{generated_at}"]
    meta_lines.extend(f"{key}：{_meta_text(value)}" for key, value in meta.items())
    lines.extend(f"> {line}" for line in meta_lines)
    lines.append("")

    for section in sections:
        heading = str(getattr(section, "heading", "") or "").strip()
        body = str(getattr(section, "body", "") or "").strip()
        table = getattr(section, "table", None)
        if not heading and not body and table is None:
            continue
        if heading:
            lines.extend([f"## {heading}", ""])
        if body:
            lines.extend([body, ""])
        if table is not None:
            lines.extend([_render_table(table), ""])

    lines.extend(
        [
            "---",
            "",
            f"数据来源：{_data_source(meta)}",
            f"生成时间：{generated_at}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_markdown(path: str | Path, text: str) -> Path:
    """把 Markdown 写到磁盘（UTF-8，无 BOM），返回写入的路径。

    父目录不存在时自动建。用 UTF-8 而不是系统默认编码：报告里有中文，
    Windows 上 ``open()`` 的默认编码不是 UTF-8，换个环境打开就会乱码。
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return target


# ---------------------------------------------------------------------------
# 具体报告
# ---------------------------------------------------------------------------


def _record_area_km2(record: Any) -> float | None:
    """面积（km²）。``area_m2`` 优先——它是 LakeRecord 的原始字段。

    不能直接访问 ``record.area_km2``：``area_m2`` 为 None 时那个 property
    会抛 TypeError，而"没量到"应该是一种可渲染的结果，不是程序错误。
    """
    area_m2 = _as_float(_field(record, "area_m2"))
    if area_m2 is not None:
        return area_m2 / 1e6
    return _as_float(_field(record, "area_km2"))


def build_lake_report(
    records: Sequence["LakeRecord"],
    *,
    title: str,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """湖泊清单报告：编号 / 面积 / 质心 / 风险等级 / 判据摘要。

    空列表输出「无数据」，不输出一张只有表头的空表——"没提取到湖"是一个
    结论，不是一个等待填数的模板。
    """
    items = list(records)

    if not items:
        body = (
            "**无数据**：本次没有提取到任何湖泊。\n\n"
            "这表示「没提出来」，不等于「面积为零」，也不等于「这里没有湖」；"
            "换阈值或换影像后应重跑，而不是把空表当成结果。"
        )
        return build_markdown(
            title=title,
            sections=[ReportSection(heading="湖泊清单", body=body)],
            meta=meta,
        )

    headers = ["编号", "面积 km²", "质心经纬度", "风险等级", "判据摘要"]
    rows: list[list[str]] = []
    measured_total = 0.0
    n_missing_area = 0
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}

    for record in items:
        area = _record_area_km2(record)
        if area is None:
            n_missing_area += 1
        else:
            measured_total += area

        reasons = _as_reasons(_field(record, "risk_reasons"))
        level_raw = _level_value(_field(record, "risk_level"))
        counts[level_raw if level_raw in counts else "UNKNOWN"] += 1

        rows.append(
            [
                _field(record, "lake_id", "—"),
                _fmt_area_km2(area),
                _fmt_coord(_field(record, "centroid_lat"), _field(record, "centroid_lon")),
                _risk_level_text(level_raw, reasons),
                _reasons_text(reasons),
            ]
        )

    if n_missing_area == len(items):
        total_text = f"合计面积：{MISSING_TEXT}（{len(items)} 个湖都没有量到面积）"
    elif n_missing_area:
        total_text = (
            f"合计面积：{_fmt_area_km2(measured_total)} km²（仅统计量到面积的 "
            f"{len(items) - n_missing_area} 个湖；另有 {n_missing_area} 个湖"
            f"面积{MISSING_TEXT}，未计入合计）"
        )
    else:
        total_text = f"合计面积：{_fmt_area_km2(measured_total)} km²"

    body = "\n".join(
        [
            f"**共 {len(items)} 个湖泊。** {total_text}。",
            "",
            (
                f"风险等级分布：高风险 {counts['HIGH']}、中风险 {counts['MEDIUM']}、"
                f"低风险 {counts['LOW']}、未判定 {counts['UNKNOWN']}。"
            ),
            "",
            (
                "> 面积为「没量到」表示该湖没有可用的量测结果，不是 0；"
                "「未判定（缺数据：…）」表示判据因缺数据未评估，**不等于低风险**。"
                "面积由投影坐标系下的多边形面积换算（1 km² = 1e6 m²）。"
            ),
        ]
    )

    return build_markdown(
        title=title,
        sections=[ReportSection(heading="湖泊清单", body=body, table=ReportTable(headers, rows))],
        meta=meta,
    )


def _risk_row(item: Any) -> dict[str, Any]:
    """把 SeriesAssessment / RiskAssessment / JSON 行统一成同一组字段。"""
    if isinstance(item, Mapping):
        reasons = item.get("reasons")
        if reasons is None:
            reasons = item.get("risk_reasons")
        return {
            "date": item.get("date") or item.get("label") or "—",
            "area_km2": _as_float(item.get("area_km2")),
            "value": _as_float(item.get("value")),
            "level": item.get("level", item.get("risk_level")),
            "policy": item.get("policy", ""),
            "reasons": _as_reasons(reasons),
            "summary": str(item.get("summary", "") or ""),
        }

    assessment = getattr(item, "assessment", None)
    if assessment is not None:
        value = getattr(item, "value", None)
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        return {
            "date": str(getattr(item, "date", "") or "—"),
            "area_km2": _as_float(getattr(item, "area_km2", None)),
            "value": value if value is None or math.isfinite(value) else None,
            "level": getattr(assessment, "level", None),
            "policy": str(getattr(item, "policy", "") or ""),
            "reasons": _as_reasons(getattr(assessment, "reasons", [])),
            "summary": str(getattr(assessment, "summary", "") or ""),
        }

    # 裸的 RiskAssessment：没有 date / area / policy，但**有判据值**。
    # 早先这里把 value 写死成 None，于是 docstring 明说支持的 RiskAssessment
    # 输入会印出「没量到」——明明 criteria[0].value 有数，报告却说没量到，
    # 这是反向的诚实性错误（把量到的说成没量到），同样会误导读者。
    # 取第一条有有限数值的判据，口径与 risk_series.SeriesAssessment.value 一致。
    value: float | None = None
    for criterion in getattr(item, "criteria", None) or ():
        raw = getattr(criterion, "value", None)
        if isinstance(raw, bool):
            continue
        number = _as_float(raw)
        if number is not None:
            value = number
            break

    return {
        "date": "—",
        "area_km2": None,
        "value": value,
        "level": getattr(item, "level", None),
        "policy": "",
        "reasons": _as_reasons(getattr(item, "reasons", [])),
        "summary": str(getattr(item, "summary", "") or ""),
    }


def build_risk_report(
    assessments: Sequence[Any],
    *,
    title: str,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """逐期风险判定表。

    ``assessments`` 可以是 ``risk_series.SeriesAssessment``、``risk.RiskAssessment``，
    也可以是 ``output/risk/risk_assessments.json`` 里 ``rows`` 的字典——三种形状
    在报告里必须长得一样，否则同一批判定换个入口就会少一列。

    等级列对 UNKNOWN 写「未判定（缺数据：<原因>）」，原因取自逐条判据里
    带「缺数据 / 未评估 / 不足」的说明。
    """
    items = list(assessments)

    if not items:
        body = "**无数据**：没有可报告的判定结果。缺数据不等于判为低风险。"
        return build_markdown(
            title=title,
            sections=[ReportSection(heading="逐期判定", body=body)],
            meta=meta,
        )

    headers = ["日期", "面积 km²", "判据值", "风险等级", "参照政策", "判据摘要"]
    rows: list[list[str]] = []
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}

    for item in items:
        row = _risk_row(item)
        level_raw = _level_value(row["level"])
        counts[level_raw if level_raw in counts else "UNKNOWN"] += 1
        rows.append(
            [
                row["date"],
                _fmt_area_km2(row["area_km2"]),
                MISSING_TEXT if row["value"] is None else _fmt_number(row["value"]),
                _risk_level_text(level_raw, row["reasons"], summary=row["summary"]),
                row["policy"] or "—",
                _reasons_text(row["reasons"], row["summary"]),
            ]
        )

    body = "\n".join(
        [
            (
                f"**共 {len(items)} 期：高风险 {counts['HIGH']}、中风险 {counts['MEDIUM']}、"
                f"低风险 {counts['LOW']}、未判定 {counts['UNKNOWN']}。**"
            ),
            "",
            (
                "> 判据值是该期第一条可评估判据的实测值（冰坝为面积/平台比值，"
                "冰碛坝为面积年增速）；「没量到」表示该判据没有可评估的数值。"
                "未判定 = 缺数据，**不是低风险**。阈值与判据定义见 risk.py，"
                "本报告不重复实现判定逻辑。"
            ),
        ]
    )

    return build_markdown(
        title=title,
        sections=[ReportSection(heading="逐期判定", body=body, table=ReportTable(headers, rows))],
        meta=meta,
    )


def _pct(value: Any) -> str:
    number = _as_float(value)
    return MISSING_TEXT if number is None else f"{number * 100:.1f}%"


def _as_count(value: Any) -> int | None:
    """计数字段：非负整数才算数到，其余一律 None（渲染成「没量到」）。

    ``str(None)`` 会在表格里印出字面量 ``None``——一个缺失被当成一个值展示，
    和印成 0 是同一类错误，只是更难发现。
    """
    number = _as_float(value)
    if number is None or number < 0 or number != int(number):
        return None
    return int(number)


def _pixel_metric(pixel_counts: Any, numerator: str, denominator: Sequence[str]) -> str:
    """像元级指标。**分母为零时返回「没量到」，不是 0.0%。**

    ``accuracy._ratio()`` 在分母为零时返回 0.0 是为了 JSON 合法（nan 不是合法
    JSON），并明确要求调用方结合样本数一起看。报告如果直接印 ``0.0%``，
    等于把「没有样本」说成「全错」——和把点位召回率的分母为零写成 0.0% 是同一个坑。
    """
    num = _as_float(_field(pixel_counts, numerator))
    parts = [_as_float(_field(pixel_counts, name)) for name in denominator]
    if num is None:
        return MISSING_TEXT
    # 计数齐全时才敢判"分母为零"；只有比率、没有计数的输入无法核对分母，
    # 仍按调用方给的比率渲染——那是它明确给出的数，不是本模块补出来的。
    if all(part is not None for part in parts) and sum(parts) <= 0:
        return MISSING_TEXT
    return _pct(num)


def build_accuracy_report(report: "EvaluationReport") -> str:
    """精度评价章节（Markdown，从 ``##`` 开始，可直接塞进 ReportSection.body）。

    这一章的全部价值在于**把口径和数字绑在一起**：``point_recall`` 的分母是
    被影像覆盖的点位数，不是 ICIMOD 真值总数；它量的是点位命中，不是边界重合。
    所以章节里固定有一句「点位召回不是边界精度，不能写成 IoU/F1」，
    即使调用方只截取表格，也截不掉这句声明。
    """
    # 计数缺失（例如调用方直接把 CLI 落盘的 accuracy.json 喂进来——那份文件
    # 只有 n_windows / n_matched，没有 n_truth / n_covered）时必须是 None，
    # 不能是 0：0 会让下面的 "n_truth == 0" 分支把"没给数"说成"没有真值点位"。
    n_truth = _as_count(_field(report, "n_truth"))
    n_covered = _as_count(_field(report, "n_covered"))
    n_matched = _as_count(_field(report, "n_matched"))
    coverage_rate = _field(report, "coverage_rate")
    point_recall = _field(report, "point_recall")
    radius_km = _field(report, "radius_km")
    notes = _as_reasons(_field(report, "notes"))
    matches = _as_reasons(_field(report, "matches"))
    pixel_counts = _field(report, "pixel_counts")

    lines: list[str] = ["## 精度评价（点位级，弱真值）", ""]

    # 分母为零时比率没有意义。accuracy._ratio() 出于 JSON 合法性记 0.0，
    # 但报告不能把它印成 0.0%——那会被读成"全错"。这里显式改成"没量到"。
    if n_truth is None or n_covered is None:
        coverage_text = f"{MISSING_TEXT}（未提供真值/覆盖点数）"
        recall_text = f"{MISSING_TEXT}（未提供覆盖点数）"
    else:
        coverage_text = _pct(coverage_rate) if n_truth else f"{MISSING_TEXT}（无真值点位）"
        recall_text = (
            _pct(point_recall) if n_covered else f"{MISSING_TEXT}（无被覆盖点位）"
        )

    if n_truth is None or n_covered is None:
        # 缺计数 ≠ 计数为零。分不清这两件事时，宁可说"没量到"并列出已知的部分，
        # 也不能替调用方断言"没有真值点位"。
        known = []
        if n_matched is not None:
            known.append(f"命中 {n_matched} 个点位")
        if _as_float(point_recall) is not None:
            known.append(f"调用方给出的召回率 {_pct(point_recall)}")
        detail = ("；".join(known) + "，") if known else ""
        lines.append(
            f"**口径不全**：未提供真值点位数或被覆盖点位数（{detail}"
            "分母未知），因此不报告覆盖率与召回率百分比。"
            "缺计数不等于计数为零——需要调用方把 n_truth / n_covered 一起给出。"
        )
        lines.append("")
    elif n_truth == 0 or n_covered == 0:
        lines.append(
            "**无可评价对象**：没有真值点位，或没有任何点位被影像覆盖，"
            "召回率没有分母。此时不报告百分比——分母为零时记 0.0% 等于把"
            "「没测」说成「全错」。"
        )
        lines.append("")
    else:
        radius_text = _as_float(radius_km)
        radius_text = MISSING_TEXT if radius_text is None else _fmt_number(radius_text)
        lines.append(
            f"**点位召回率 {recall_text}**（半径 {radius_text} km，"
            f"分母 = 被影像覆盖的 {n_covered} 个点位，不是真值总数 {n_truth}）。"
        )
        lines.append("")

    radius_value = _as_float(radius_km)
    metrics = ReportTable(
        headers=["指标", "值", "说明"],
        rows=[
            ["真值点位数", MISSING_TEXT if n_truth is None else str(n_truth),
             "ICIMOD HMA GLOF 数据库点位"],
            ["影像覆盖点位数", MISSING_TEXT if n_covered is None else str(n_covered),
             "落在评估影像窗口内的点位"],
            ["覆盖率", coverage_text, "覆盖数 ÷ 真值总数"],
            ["命中点位数", MISSING_TEXT if n_matched is None else str(n_matched),
             "距最近水体边界 ≤ 半径"],
            ["点位召回率", recall_text, "命中数 ÷ 覆盖数（分母不是真值总数）"],
            [
                "召回半径 km",
                MISSING_TEXT if radius_value is None else _fmt_number(radius_value),
                "评价口径，必须随结果一起报",
            ],
        ],
    )
    lines.append(_render_table(metrics))
    lines.append("")
    lines.append(
        "> **口径声明：点位召回不是边界精度。** ICIMOD 是历史溃决事件库，"
        "提供的是事件点位而非面真值；本次没有做边界评价，所以上面的数字"
        "**不能写成 IoU 或 F1**——两者量的不是同一件事。"
    )
    lines.append("")

    if matches:
        match_rows: list[list[str]] = []
        for match in matches:
            distance = _as_float(_field(match, "distance_km"))
            # matched 缺失时不能 bool(None) 成 False——那会把"不知道命中没有"
            # 写成"未命中"，和把 UNKNOWN 写成低风险是同一类错误。
            matched_raw = _field(match, "matched")
            matched_text = (
                MISSING_TEXT if matched_raw is None
                else ("命中" if bool(matched_raw) else "未命中")
            )
            lake_id = _field(match, "lake_id")
            lake_area = _as_float(_field(match, "lake_area_km2"))
            match_rows.append(
                [
                    str(_field(match, "site_id", "—")),
                    MISSING_TEXT if distance is None else f"{distance:.4f}",
                    matched_text,
                    "—" if lake_id is None else str(lake_id),
                    _fmt_area_km2(lake_area),
                ]
            )
        lines.extend(
            [
                "### 逐点位匹配",
                "",
                _render_table(
                    ReportTable(
                        headers=["点位", "距离 km", "是否命中", "最近湖编号", "最近湖面积 km²"],
                        rows=match_rows,
                    )
                ),
                "",
                (
                    f"> 距离为「{MISSING_TEXT}」表示该点位附近一个水体都没提取到，"
                    "不是距离为 0。"
                ),
                "",
            ]
        )
    else:
        lines.extend(["### 逐点位匹配", "", "**无数据**：没有逐点位匹配记录。", ""])

    if notes:
        lines.extend(["### 评价口径与已知局限", ""])
        lines.extend(f"- {_clean_reason(note)}" for note in notes)
        lines.append("")

    if pixel_counts is not None:
        # 分母用混淆矩阵的计数，而不是只看比率：只有知道分母非零，
        # "0.0%" 才能被读成"算出来是零"，否则它是 accuracy._ratio() 的缺省值。
        lines.extend(
            [
                "### 像元级指标（与上面的点位召回不是同一口径）",
                "",
                _render_table(
                    ReportTable(
                        headers=["精确率", "召回率", "F1", "IoU"],
                        rows=[
                            [
                                _pixel_metric(pixel_counts, "precision", ("true_positive", "false_positive")),
                                _pixel_metric(pixel_counts, "recall", ("true_positive", "false_negative")),
                                # F1 = 2PR/(P+R)，分母是 P+R：TP=0 时 P+R=0，
                                # f1 无定义（accuracy._ratio 记 0.0 只是为了 JSON 合法）。
                                _pixel_metric(pixel_counts, "f1", ("true_positive",)),
                                _pixel_metric(pixel_counts, "iou", ("true_positive", "false_positive", "false_negative")),
                            ]
                        ],
                    )
                ),
                "",
                (
                    "> 这四个数来自像元混淆矩阵，需要逐像元的真值掩膜；"
                    "它们与「点位召回率」不可互相替代，报告里必须分开写。"
                ),
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Markdown -> 自包含 HTML
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f4f6f9;
  color: #1c2430;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei",
               "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif;
  font-size: 16px;
  line-height: 1.75;
}
main.report {
  max-width: 1080px;
  margin: 0 auto;
  padding: 44px 40px 64px;
  background: #ffffff;
  box-shadow: 0 1px 3px rgba(20, 30, 45, 0.08);
}
h1, h2, h3, h4 { line-height: 1.35; color: #101a28; }
h1 { font-size: 28px; margin: 0 0 18px; padding-bottom: 12px; border-bottom: 2px solid #e2e7ee; }
h2 { font-size: 21px; margin: 36px 0 12px; padding-left: 10px; border-left: 4px solid #4b6b96; }
h3 { font-size: 17px; margin: 26px 0 10px; }
p { margin: 10px 0; }
ul { margin: 10px 0; padding-left: 24px; }
li { margin: 4px 0; }
strong { color: #0f1a2a; }
code {
  padding: 1px 5px;
  border-radius: 3px;
  background: #eef1f5;
  font-family: Consolas, "SFMono-Regular", Menlo, monospace;
  font-size: 0.92em;
}
blockquote {
  margin: 14px 0;
  padding: 8px 14px;
  border-left: 4px solid #c4cedb;
  background: #f8f9fb;
  color: #46525f;
}
blockquote p { margin: 4px 0; }
hr { border: none; border-top: 1px solid #dfe4ea; margin: 32px 0 18px; }
.table-wrap { width: 100%; overflow-x: auto; margin: 14px 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { border: 1px solid #dde2e9; padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #eef2f7; font-weight: 600; white-space: nowrap; }
tbody tr:nth-child(even) { background: #fafbfc; }
@media (max-width: 720px) {
  main.report { padding: 24px 16px 48px; }
  h1 { font-size: 23px; }
  body { font-size: 15px; }
}
""".strip()


def _inline(text: str) -> str:
    """行内格式：先转义 HTML，再处理 ``code`` 与 ``**粗体**``。

    转义必须在格式替换之前：``<script>`` 变成 ``&lt;script&gt;`` 之后，
    即使它落在粗体里也破坏不了文档结构。
    """
    parts = re.split(r"(`[^`]*`)", text)
    out: list[str] = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{_html.escape(part[1:-1], quote=False)}</code>")
        else:
            escaped = _html.escape(part, quote=False)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            out.append(escaped)
    return "".join(out)


def _is_separator(line: str) -> bool:
    """表格的第二行：``| --- | :---: |``。

    必须先把**所有**竖线去掉再判：只 strip 两端的话，中间的分隔竖线会留在
    集合里，导致 ``set(...) <= set(":-")`` 永远为假，整张表被当成普通段落。
    """
    stripped = line.strip().replace("|", "").replace(" ", "")
    return bool(stripped) and set(stripped) <= set(":-") and "-" in stripped


def _split_row(line: str) -> list[str]:
    """按未转义的 ``|`` 切单元格，``\\|`` 还原成字面竖线。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]

    cells: list[str] = []
    buffer: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buffer.append(text[i + 1])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buffer).strip())
            buffer = []
            i += 1
            continue
        buffer.append(ch)
        i += 1
    cells.append("".join(buffer).strip())
    return cells


def _render_html_table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{_inline(cell)}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "\n".join(body_rows)
    return (
        '<div class="table-wrap"><table>\n'
        f"<thead><tr>{head}</tr></thead>\n"
        f"<tbody>\n{body}\n</tbody>\n"
        "</table></div>"
    )


def _markdown_to_body(markdown_text: str) -> str:
    """极简 Markdown → HTML 片段。只认报告里真正会用到的那几种块。"""
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 表格：当前行以 | 开头，下一行是分隔行
        if stripped.startswith("|") and i + 1 < n and _is_separator(lines[i + 1]):
            header = _split_row(lines[i])
            rows: list[list[str]] = []
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                row = _split_row(lines[i])
                if len(row) != len(header):
                    raise ValueError(
                        f"Markdown 表格第 {len(rows) + 1} 行有 {len(row)} 列，"
                        f"表头是 {len(header)} 列；拒绝渲染会错位的表"
                    )
                rows.append(row)
                i += 1
            out.append(_render_html_table(header, rows))
            continue

        # 标题
        match = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if match:
            level = len(match.group(1))
            out.append(f"<h{level}>{_inline(match.group(2).strip())}</h{level}>")
            i += 1
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            out.append("<hr>")
            i += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append("<blockquote><p>" + "<br>\n".join(_inline(q) for q in quote) + "</p></blockquote>")
            continue

        # 无序列表
        if re.match(r"^[-*]\s+", stripped):
            items: list[str] = []
            while i < n and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(_inline(re.sub(r"^[-*]\s+", "", lines[i].strip())))
                i += 1
            out.append("<ul>\n" + "\n".join(f"<li>{item}</li>" for item in items) + "\n</ul>")
            continue

        # 段落：连续普通行，行内换行用 <br>
        paragraph: list[str] = []
        while i < n:
            current = lines[i].strip()
            if not current:
                break
            if (
                re.match(r"^(#{1,4})\s+", current)
                or current.startswith(">")
                or current in ("---", "***", "___")
                or re.match(r"^[-*]\s+", current)
                or (current.startswith("|") and i + 1 < n and _is_separator(lines[i + 1]))
            ):
                break
            paragraph.append(_inline(current))
            i += 1
        if not paragraph:
            # 兜底：没有任何分支认领这一行（例如以 | 开头却不成表的孤行）。
            # 不兜底的话 while 不前进，会在这里死循环——报告模块卡死比排版难看严重得多。
            out.append("<p>" + _inline(stripped) + "</p>")
            i += 1
            continue
        out.append("<p>" + "<br>\n".join(paragraph) + "</p>")

    return "\n".join(out)


def _first_heading(markdown_text: str) -> str:
    for line in markdown_text.splitlines():
        match = re.match(r"^#\s+(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    return ""


def to_html(markdown_text: str, *, title: str) -> str:
    """Markdown → 单文件自包含 HTML。

    Parameters
    ----------
    markdown_text
        ``build_markdown()`` 或三个 ``build_*_report()`` 的输出。
    title
        ``<title>`` 与浏览器标签页标题。空字符串时退回正文第一个一级标题，
        再没有就用「报告」。

    输出不含任何外部引用：没有 ``<link>``、``<script>``、``<img>``、
    ``@import``，也没有指向 http(s) 的 ``href`` / ``src``。样式全部在
    一个 ``<style>`` 里，断网可看。
    """
    body = _markdown_to_body(markdown_text)
    page_title = str(title or "").strip() or _first_heading(markdown_text) or "报告"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_html.escape(page_title, quote=False)}</title>\n"
        "<style>\n"
        f"{_CSS}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        '<main class="report">\n'
        f"{body}\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )
