"""时序曲线的视图模型：Python 数据 <-> QML 图表。

为什么需要一个 Controller，而不是把 list[SeriesPoint] 直接暴露给 QML
--------------------------------------------------------------------
QML 的 ChartView 吃不了 Python dataclass；它能稳定消费的是 QVariantList
里"键为驼峰、值为标量"的字典。``set_series()`` 负责这一次形状变换，
QML 那边只认 date / areaKm2 / validPx / validFrac / cloud 五个键。

平台面积 plateau 用一个 float 表达"有没有"：无平台时是 NaN，过线就是
JS 的 NaN，QML 用 ``Number.isNaN()`` 判断即可，不需要额外的布尔位。

颜色也从这里出：QML 不能 import Python，style.py 的风险色经
``palette`` 属性注入（在契约要求的四个属性之外附加的一个常量属性，
见下），SeriesChart.qml 不自造任何色值，风险色全项目只有 style.py
这一份。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from PySide6.QtCore import Property, QObject, Signal, Slot

from glof.core.timeseries import SeriesPoint
from glof.ui.style import RISK_COLORS

__all__ = ["SeriesController"]

# 图表语义色，全部取自 style.py（风险色唯一来源），不在此处另造色值：
#   plateau   平台线 -> MEDIUM 橙：是一条"上限/阈值"性质的线
#   event     事件年参考线与年份标注 -> HIGH 红：溃决事件本身是危险事件
#   highlight 当前选中的观测点 -> HIGH 红：与事件色一致，"看这里"
#   muted     空态提示文字 -> UNKNOWN 灰：中性提示，不表达任何风险
_PALETTE: dict[str, str] = {
    "plateau": RISK_COLORS["MEDIUM"],
    "event": RISK_COLORS["HIGH"],
    "highlight": RISK_COLORS["HIGH"],
    "muted": RISK_COLORS["UNKNOWN"],
}


class SeriesController(QObject):
    """面积时序的视图模型，经上下文属性 ``app.series`` 供 QML 读取。

    一个 Controller 实例就是一条曲线的全部状态；换数据用整体替换
    （``set_series``），不做增量——曲线点最多几百个，全量替换更简单，
    也不会出现"改了一半"的中间态。

    Q_PROPERTY
    ----------
    points : QVariantList
        元素为 ``{"date": str, "areaKm2": float, "validPx": int,
        "validFrac": float, "cloud": float}``，按日期升序。
    plateau : float
        满蓄平台面积（km²），无平台时 NaN。
    eventYears : QVariantList[int]
        需要在曲线上立参考线的年份（如溃决事件年）。
    highlighted : str
        高亮观测点的日期（"YYYY-MM-DD"），"" 表示无。
    palette : QVariantMap（契约之外的附加常量属性）
        语义色映射，值全部来自 ``glof.ui.style.RISK_COLORS``。
    """

    pointsChanged = Signal()
    plateauChanged = Signal()
    eventYearsChanged = Signal()
    highlightedChanged = Signal()

    #: QML 图表点击某点后回抛的日期，"YYYY-MM-DD"。
    pointClicked = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._points: list[dict[str, Any]] = []
        self._plateau: float = float("nan")
        self._event_years: list[int] = []
        self._highlighted: str = ""

    # ---- QML 只读属性的取值函数 ----

    def _get_points(self) -> list[dict[str, Any]]:
        return self._points

    def _get_plateau(self) -> float:
        return self._plateau

    def _get_event_years(self) -> list[int]:
        return self._event_years

    def _get_highlighted(self) -> str:
        return self._highlighted

    def _get_palette(self) -> dict[str, str]:
        return dict(_PALETTE)

    points = Property("QVariantList", _get_points, notify=pointsChanged)
    plateau = Property(float, _get_plateau, notify=plateauChanged)
    eventYears = Property("QVariantList", _get_event_years, notify=eventYearsChanged)
    highlighted = Property(str, _get_highlighted, notify=highlightedChanged)
    # 常量属性：色板在进程内不会变，QML 侧无需求值通知。
    palette = Property("QVariantMap", _get_palette, constant=True)

    # ---- Python 侧的状态入口 ----

    def set_series(
        self,
        points: Sequence[SeriesPoint],
        plateau_km2: float,
        event_years: Sequence[int] = (),
    ) -> None:
        """整体替换序列数据并广播变更。

        plateau_km2 传 NaN（或 None）表示"估不出平台"，
        对应 ``timeseries.estimate_plateau()`` 的 NaN 返回值。
        """
        ordered = sorted(points, key=lambda p: p.date)
        self._points = [
            {
                "date": str(p.date),
                "areaKm2": float(p.area_km2),
                "validPx": int(p.valid_px),
                "validFrac": float(p.valid_frac),
                "cloud": float(p.cloud),
            }
            for p in ordered
        ]
        self._plateau = float("nan") if plateau_km2 is None else float(plateau_km2)
        self._event_years = [int(y) for y in event_years]
        self.pointsChanged.emit()
        self.plateauChanged.emit()
        self.eventYearsChanged.emit()

    def highlight(self, date: str) -> None:
        """把某个日期的观测点设为高亮；空串等于取消高亮。"""
        value = str(date or "")
        if value == self._highlighted:
            return
        self._highlighted = value
        self.highlightedChanged.emit()

    def clear(self) -> None:
        """清空到初始态：points 空、plateau NaN、事件年空、取消高亮。"""
        self._points = []
        self._plateau = float("nan")
        self._event_years = []
        if self._highlighted:
            self._highlighted = ""
            self.highlightedChanged.emit()
        self.pointsChanged.emit()
        self.plateauChanged.emit()
        self.eventYearsChanged.emit()

    def has_plateau(self) -> bool:
        """当前平台值是否可用（NaN 判断收在这里，Python 侧别再各写一遍）。"""
        return math.isfinite(self._plateau)

    # ---- QML 侧入口 ----

    @Slot(str)
    def chartClicked(self, date: str) -> None:
        """QML MouseArea 就近取点后调用，原样把日期抛给 Python 侧。"""
        self.pointClicked.emit(str(date))
