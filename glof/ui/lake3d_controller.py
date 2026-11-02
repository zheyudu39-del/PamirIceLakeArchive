"""3D 湖面回放的视图模型：DEM 地形网格 + 每期水位，供 QML 的 View3D 消费。

为什么单独一个 controller
------------------------
QML 的 View3D 不能直接吃 numpy 数组。这里把 DEM 降采样成一张小网格
（默认 96×96 顶点），连同每期的相对水位、日期、面积一起打包成
QVariantList，QML 侧的 Repeater/InstancedMesh 只认"键为驼峰、值为标量"的字典。

**这一层只做搬运和降采样，不做任何判定**。水位从哪来、为什么是这个数，
全部由 ``glof.core.lake_3d.LevelAreaCurve`` 决定；controller 不重新解释。

诚实边界（原样透传到 UI）
------------------------
- ``levelSource`` 会带出曲线的来历说明，界面必须显示，不能只画不说。
- 某一期水位算不出来（面积超出曲线范围）时，该期 ``relativeLevel`` 是
  NaN，QML 要显示"水位未知"，**不能画成 0**——0 在这里是"湖空了"，
  与"没算出来"是两件完全不同的事。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
from PySide6.QtCore import Property, QObject, Signal, Slot

from glof.core.lake_3d import LevelAreaCurve

__all__ = ["Lake3DController", "build_terrain_grid"]


def build_terrain_grid(
    dem: np.ndarray,
    *,
    max_vertices: int = 96,
    z_scale: float = 1.0,
) -> dict[str, Any]:
    """把 DEM 降采样成 3D 视图能吃的规则网格。

    Parameters
    ----------
    dem
        二维高程数组（米），NaN 表示无数据。
    max_vertices
        单边最多保留多少顶点。96×96 ≈ 9k 顶点，QML 侧用
        InstancedMesh 或 PositionBuilder 都能流畅画；再高对答辩演示
        没有可见收益，反而拖慢软件渲染。
    z_scale
        高程放大系数。默认 1.0（真实比例）——放大视觉上是好看，但会
        让"湖有多深"看起来比实际夸张，和项目"不美化数字"的调性冲突。

    Returns
    -------
    dict
        ``heights``：扁平化的行优先高度列表（米，NaN 换成该网格的最小值
        以免 QML 拿到 NaN 后整块不画）；
        ``rows`` / ``cols``：网格尺寸；
        ``zMin`` / ``zMax``：原始高程范围（米），供着色和标注；
        ``nanCount``：原始 NaN 顶点数，如实报出来。
    """
    if dem.ndim != 2:
        raise ValueError(f"dem 必须是二维数组，得到 shape={dem.shape}")

    rows, cols = dem.shape
    step_r = max(1, int(math.ceil(rows / max_vertices)))
    step_c = max(1, int(math.ceil(cols / max_vertices)))
    sub = dem[::step_r, ::step_c]

    finite = np.isfinite(sub)
    nan_count = int(np.count_nonzero(~finite))
    if np.any(finite):
        z_min = float(np.nanmin(sub))
        z_max = float(np.nanmax(sub))
        # NaN 用最低值填充：QML 里 NaN 会让整块几何退化，填充后至少
        # 地形是连续的。nanCount 如实报出，界面可以写"有 N 个顶点无数据"。
        filled = np.where(finite, sub, z_min)
    else:
        z_min = z_max = 0.0
        filled = np.zeros_like(sub)

    if z_scale != 1.0:
        filled = z_min + (filled - z_min) * z_scale

    return {
        "heights": [float(v) for v in filled.ravel()],
        "rows": int(filled.shape[0]),
        "cols": int(filled.shape[1]),
        "zMin": z_min,
        "zMax": z_max,
        "nanCount": nan_count,
    }


class Lake3DController(QObject):
    """3D 回放的状态：地形网格 + 每期水位。经上下文属性 ``app.lake3d`` 暴露。"""

    readyChanged = Signal()
    framesChanged = Signal()
    currentIndexChanged = Signal()
    messageChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ready = False
        self._rows = 0
        self._cols = 0
        self._heights: list[float] = []
        self._z_min = float("nan")
        self._z_max = float("nan")
        self._nan_count = 0
        self._frames: list[dict[str, Any]] = []
        self._current_index = -1
        self._level_source = ""
        self._message = ""
        # 地形纹理 URL。由 AppState 在烘完图后写入（image://terrain/...）。
        # 空串表示"没有纹理"，QML 退回纯色材质。
        self._texture_url = ""
        self._texture_source = ""
        # 湖心在 DEM 网格里的归一化位置（0..1）。QML 据此把湖面放到正确
        # 的谷底位置，而不是场景原点——DEM 窗口的中心不一定是湖心
        # （Kyagar 实测湖心在 col 0.45 / row 0.57）。
        self._seed_x = 0.5
        self._seed_y = 0.5
        # 水位曲线的跨度（米）。QML 用它把"相对水位 0..1"换成真实米数，
        # 不能用整幅 DEM 的 zMax-zMin——那个是地形高差（约 2000 m），
        # 拿它乘水位比例会把湖面抬到山顶之上。
        self._level_span_m = 1.0

    # ---- Q_PROPERTY ----

    def _get_ready(self) -> bool:
        return self._ready

    def _get_rows(self) -> int:
        return self._rows

    def _get_cols(self) -> int:
        return self._cols

    def _get_heights(self) -> list[float]:
        return self._heights

    def _get_z_min(self) -> float:
        return self._z_min

    def _get_z_max(self) -> float:
        return self._z_max

    def _get_nan_count(self) -> int:
        return self._nan_count

    def _get_frames(self) -> list[dict[str, Any]]:
        return self._frames

    def _get_current_index(self) -> int:
        return self._current_index

    def _get_level_source(self) -> str:
        return self._level_source

    def _get_texture_url(self) -> str:
        return self._texture_url

    def _get_seed_x(self) -> float:
        return self._seed_x

    def _get_seed_y(self) -> float:
        return self._seed_y

    def _get_level_span_m(self) -> float:
        return self._level_span_m

    def _get_message(self) -> str:
        return self._message

    ready = Property(bool, _get_ready, notify=readyChanged)
    rows = Property(int, _get_rows, notify=readyChanged)
    cols = Property(int, _get_cols, notify=readyChanged)
    heights = Property("QVariantList", _get_heights, notify=readyChanged)
    zMin = Property(float, _get_z_min, notify=readyChanged)
    zMax = Property(float, _get_z_max, notify=readyChanged)
    nanCount = Property(int, _get_nan_count, notify=readyChanged)
    frames = Property("QVariantList", _get_frames, notify=framesChanged)
    currentIndex = Property(int, _get_current_index, notify=currentIndexChanged)
    # levelSource 在 set_frames 里赋值（曲线的 source 随帧一起到），所以
    # notify 必须是 framesChanged 而不是 readyChanged。用错信号的表现很隐蔽：
    # QML 绑定初次求值时它还是空串，之后不再重算——标签存在、可见，
    # 但永远是空文本（实测踩过）。
    levelSource = Property(str, _get_level_source, notify=framesChanged)
    message = Property(str, _get_message, notify=messageChanged)
    # 地形纹理 URL。换地形时才变，所以挂 readyChanged。
    textureUrl = Property(str, _get_texture_url, notify=readyChanged)
    # 湖心在网格里的归一化坐标（0..1），用于把湖面放到正确位置
    seedX = Property(float, _get_seed_x, notify=readyChanged)
    seedY = Property(float, _get_seed_y, notify=readyChanged)
    # 水位曲线的跨度（米）。QML 换算水面高度必须用它，不能用 DEM 高差。
    levelSpanM = Property(float, _get_level_span_m, notify=framesChanged)

    # ---- 状态入口 ----

    def set_terrain(
        self,
        grid: dict[str, Any],
        texture_url: str = "",
        seed_xy: tuple[float, float] | None = None,
    ) -> None:
        """装载地形网格（``build_terrain_grid`` 的输出）。

        ``texture_url`` 是可选的烘好山体阴影的贴图 URL。给空串时 QML 退回
        纯色材质——**不会因此假装地形有明暗**，只是视觉上平一些。

        ``seed_xy`` 是湖心在网格里的归一化位置 ``(x, y)``，各取 0..1。
        不给时用网格中心——但 DEM 窗口的中心不一定是湖心，QML 会把湖面
        画到错误的位置（实测 Kyagar 湖心在 col 0.45 / row 0.57）。
        """
        self._rows = int(grid["rows"])
        self._cols = int(grid["cols"])
        self._heights = list(grid["heights"])
        self._z_min = float(grid["zMin"])
        self._z_max = float(grid["zMax"])
        self._nan_count = int(grid["nanCount"])
        self._texture_url = str(texture_url)
        if seed_xy is not None:
            self._seed_x = float(seed_xy[0])
            self._seed_y = float(seed_xy[1])
        else:
            self._seed_x = 0.5
            self._seed_y = 0.5
        self._ready = self._rows > 1 and self._cols > 1
        self.readyChanged.emit()

    def set_frames(
        self,
        dates: Sequence[str],
        areas_km2: Sequence[float],
        curve: LevelAreaCurve,
    ) -> None:
        """按实测面积序列生成逐期帧。

        水位算不出来的期保留在序列里，``relativeLevel`` 为 NaN——
        丢掉会让时间轴跳期，用户以为漏采了观测。
        """
        if not (len(dates) == len(areas_km2)):
            raise ValueError("dates 与 areas_km2 长度不一致")
        frames: list[dict[str, Any]] = []
        for date, area in zip(dates, areas_km2):
            a = float(area)
            frac = curve.fraction_for_area(a)
            level = curve.level_for_area(a)
            frames.append({
                "date": str(date),
                "areaKm2": a,
                # NaN 表示"这一期水位不可用"，不是 0
                "relativeLevel": float("nan") if frac is None else float(frac),
                "absoluteLevel": float("nan") if level is None else float(level),
                "levelKnown": frac is not None,
            })
        self._frames = frames
        self._level_source = curve.source
        # 曲线跨度（米）：QML 换算水面高度用这个，不是 DEM 的 zMax-zMin
        self._level_span_m = (
            float(curve.levels[-1] - curve.levels[0])
            if curve.valid and len(curve.levels) >= 2 else 1.0
        )
        self._current_index = len(frames) - 1 if frames else -1
        self.framesChanged.emit()
        self.currentIndexChanged.emit()

        unknown = [f["date"] for f in frames if not f["levelKnown"]]
        if unknown:
            self._set_message(
                f"{len(unknown)} 期水位算不出来（面积超出 DEM 曲线范围）："
                + "、".join(unknown[:3])
                + ("…" if len(unknown) > 3 else "")
            )
        else:
            self._set_message("")

    @Slot(int)
    def setCurrentIndex(self, index: int) -> None:
        """切换当前帧（时间轴拖动/点击）。越界夹到合法范围。"""
        if not self._frames:
            return
        clamped = max(0, min(len(self._frames) - 1, int(index)))
        if clamped != self._current_index:
            self._current_index = clamped
            self.currentIndexChanged.emit()

    @Slot()
    def clear(self) -> None:
        """清空（换湖/重新检索时调用）。"""
        self._ready = False
        self._heights = []
        self._rows = self._cols = 0
        self._frames = []
        self._current_index = -1
        self._level_source = ""
        self._texture_url = ""
        self._set_message("")
        self.readyChanged.emit()
        self.framesChanged.emit()
        self.currentIndexChanged.emit()

    def _set_message(self, text: str) -> None:
        if self._message != text:
            self._message = text
            self.messageChanged.emit()
