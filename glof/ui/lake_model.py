"""湖泊/风险列表的数据模型——把 core 的 LakeRecord 摆进 QML TableView。

为什么是 QAbstractTableModel 而不是 QQmlListProperty / ListModel
----------------------------------------------------------------
数据本体是 ``glof.core.water.LakeRecord``（dataclass），模型直接持有引用，
换数据就是一次 ``set_records()`` 的 begin/endResetModel，不必把字段逐个
拷进 JS 对象。表格语义（行×列 + 水平表头）与 TableView / HorizontalHeaderView
天然对齐，中文列名只写在 ``headerData()`` 一处。

为什么数字格式化放 data() 而不是 QML delegate
----------------------------------------------
面积 4 位小数、周长 2 位小数是**统计口径**，要和 ``io.save_csv`` /
``save_geojson`` 的 properties 保持同一套规则；散落到 delegate 里迟早出现
"表格与导出数字对不上"的答辩事故。

为什么颜色经模型带出而不是在 QML 写死
--------------------------------------
等级色统一来自 ``style.risk_color()``，判据明细的"命中/未命中/缺数据"三色
来自 ``style.CRITERION_STATUS_COLORS``，以只读属性（hitColor / missColor /
unknownColor）暴露给 QML——style.py 的注释说得很清楚：两处各写一份迟早漂移。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Property,
    Qt,
    Slot,
)

from ..core.water import LakeRecord
from .style import CRITERION_STATUS_COLORS, risk_color

# 水平表头。列顺序与 data() 的 DisplayRole 分支一一对应。
_HEADERS = ("ID", "面积 km²", "周长 km", "平均NDWI", "经度", "纬度", "风险等级")
_N_COLUMNS = len(_HEADERS)


class LakeModel(QAbstractTableModel):
    """喂 QML TableView 的湖泊表。行序即 extract_lakes 的返回序（按面积降序）。"""

    # 角色号集中声明：roleNames() 里的名字与这里的键一一对应，
    # QML delegate 中以 model.lakeId / model.color 等访问。
    ROLE_LAKE_ID = Qt.ItemDataRole.UserRole + 1
    ROLE_AREA_KM2 = Qt.ItemDataRole.UserRole + 2
    ROLE_PERIMETER_KM = Qt.ItemDataRole.UserRole + 3
    ROLE_MEAN_NDWI = Qt.ItemDataRole.UserRole + 4
    ROLE_LON = Qt.ItemDataRole.UserRole + 5
    ROLE_LAT = Qt.ItemDataRole.UserRole + 6
    ROLE_LEVEL = Qt.ItemDataRole.UserRole + 7
    ROLE_COLOR = Qt.ItemDataRole.UserRole + 8
    ROLE_REASONS = Qt.ItemDataRole.UserRole + 9
    ROLE_N_PIXELS = Qt.ItemDataRole.UserRole + 10

    _ROLE_NAMES = {
        ROLE_LAKE_ID: b"lakeId",
        ROLE_AREA_KM2: b"areaKm2",
        ROLE_PERIMETER_KM: b"perimeterKm",
        ROLE_MEAN_NDWI: b"meanNdwi",
        ROLE_LON: b"lon",
        ROLE_LAT: b"lat",
        ROLE_LEVEL: b"level",
        ROLE_COLOR: b"color",
        ROLE_REASONS: b"reasons",
        ROLE_N_PIXELS: b"nPixels",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._records: list[LakeRecord] = []

    # ---- 契约方法 ------------------------------------------------------

    def set_records(self, records: Iterable[LakeRecord]) -> None:
        """整体换数据。逐行 dataChanged 会让表格抖 N 次，reset 一次到位。"""
        self.beginResetModel()
        self._records = list(records)
        self.endResetModel()

    def clear(self) -> None:
        self.set_records(())

    def record_at(self, row: int) -> LakeRecord | None:
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    @Slot(int, result=int)
    def row_of(self, lake_id: int) -> int:
        """lake_id -> 行号，找不到返回 -1。表规模是"一景几十个湖"，线性扫足够。

        带 @Slot 是因为 QML 端的 selectRow 也要靠它反查行号。
        """
        for row, rec in enumerate(self._records):
            if rec.lake_id == lake_id:
                return row
        return -1

    @Slot(int, result="QVariantList")
    def reasons_at(self, row: int) -> list[str]:
        """详情面板用：某行 risk_reasons 原文（QML 拿不到 dataclass，只能走这里）。"""
        rec = self.record_at(row)
        return list(rec.risk_reasons) if rec is not None else []

    def update_risk(self, lake_id: int, level: str, reasons: list[str]) -> bool:
        """单行更新风险等级与判据（时序判定回流的入口），找到返回 True。

        为什么不 set_records 整表 reset：reset 会把 TableView 滚动位置、
        选中行与详情面板一起冲掉，而这里只有一行的等级/颜色变了。
        就地改 LakeRecord（模型持有的是引用，data() 下次取值即新值）+
        发一次 dataChanged，表格只重绘这一行。返回 False 表示该 id 不在
        表里（如换景后旧选中），调用方据此跳过，不猜。
        """
        row = -1
        for i, rec in enumerate(self._records):
            if rec.lake_id == lake_id:
                row = i
                break
        if row < 0:
            return False
        rec = self._records[row]
        rec.risk_level = str(level)
        rec.risk_reasons = [str(r) for r in reasons]
        self.dataChanged.emit(
            self.index(row, 0),
            self.index(row, _N_COLUMNS - 1),
            [int(Qt.ItemDataRole.DisplayRole), self.ROLE_LEVEL,
             self.ROLE_COLOR, self.ROLE_REASONS],
        )
        return True

    # ---- 判据明细三色：与全局配色同源，QML 端不写死色值 ----------------

    @Property(str, constant=True)
    def hitColor(self) -> str:
        return CRITERION_STATUS_COLORS["HIT"]

    @Property(str, constant=True)
    def missColor(self) -> str:
        return CRITERION_STATUS_COLORS["MISS"]

    @Property(str, constant=True)
    def unknownColor(self) -> str:
        return CRITERION_STATUS_COLORS["UNKNOWN"]

    # ---- QAbstractTableModel 必备 --------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else _N_COLUMNS

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section] if 0 <= section < _N_COLUMNS else None
        return str(section + 1)

    def roleNames(self) -> dict[int, bytes]:
        # display 一并保留：普通文本列的 delegate 直接读 model.display
        names = {int(Qt.ItemDataRole.DisplayRole): b"display"}
        names.update({int(role): name for role, name in self._ROLE_NAMES.items()})
        return names

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        rec = self.record_at(index.row())
        if rec is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(index.column(), rec)
        if role == self.ROLE_LAKE_ID:
            return rec.lake_id
        if role == self.ROLE_AREA_KM2:
            return rec.area_km2
        if role == self.ROLE_PERIMETER_KM:
            return rec.perimeter_m / 1000.0
        if role == self.ROLE_MEAN_NDWI:
            return rec.mean_ndwi
        if role == self.ROLE_LON:
            return rec.centroid_lon
        if role == self.ROLE_LAT:
            return rec.centroid_lat
        if role == self.ROLE_LEVEL:
            # 只给等级字符串；着色交给 QML 用 color 角色（style.risk_color）
            return rec.risk_level
        if role == self.ROLE_COLOR:
            return risk_color(rec.risk_level)
        if role == self.ROLE_REASONS:
            return list(rec.risk_reasons)
        if role == self.ROLE_N_PIXELS:
            return rec.n_pixels
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        # 只读表：不给 ItemIsEditable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ---- 显示文本 ------------------------------------------------------

    @staticmethod
    def _display_text(column: int, rec: LakeRecord) -> str:
        if column == 0:
            return str(rec.lake_id)
        if column == 1:
            # 表格给人看，4 位足够；导出精度口径见 io.save_csv（6 位）
            return f"{rec.area_km2:.4f}"
        if column == 2:
            return f"{rec.perimeter_m / 1000.0:.2f}"
        if column == 3:
            if not math.isfinite(rec.mean_ndwi):
                return "-"  # NaN 不该渲染成 "nan"
            return f"{rec.mean_ndwi:.4f}"
        if column == 4:
            return f"{rec.centroid_lon:.4f}"
        if column == 5:
            return f"{rec.centroid_lat:.4f}"
        if column == 6:
            return rec.risk_level or "-"
        return ""
