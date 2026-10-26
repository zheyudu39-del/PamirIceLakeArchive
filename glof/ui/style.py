"""界面层的颜色与样式常量。

风险等级配色被地图多边形（map_canvas）和风险列表（lake_table）共用，
改这里两处同时变——两处各写一份迟早会漂移。

配色的语义约束：UNKNOWN 用灰色，**绝不能给绿色系**。"无法判定（数据不足）"
不是"安全"，这是 risk.py 诚实性原则（缺数据不判低）在配色上的延续；
把 UNKNOWN 涂绿等于在界面上把"不知道"翻译成"没问题"。
"""

from __future__ import annotations

# 风险等级 -> 颜色（hex）。键与 risk.RiskLevel 的值一致。
RISK_COLORS: dict[str, str] = {
    "HIGH": "#d93025",     # 红：全部判据命中
    "MEDIUM": "#e8710a",   # 橙：有命中但有缺口
    "LOW": "#1e8e3e",      # 绿：全部判据未命中且无缺口
    "UNKNOWN": "#9aa0a6",  # 灰：缺数据，未评估/无法判定
}

# 详情面板里"命中 / 未命中 / 缺数据"的着色，对应 risk.Criterion.status
CRITERION_STATUS_COLORS: dict[str, str] = {
    "HIT": "#d93025",
    "MISS": "#1e8e3e",
    "UNKNOWN": "#9aa0a6",
}


def risk_color(level: str | None) -> str:
    """风险等级取色。未知等级按 UNKNOWN 处理（宁可显示成"不确定"，不猜）。"""
    return RISK_COLORS.get((level or "").upper(), RISK_COLORS["UNKNOWN"])
