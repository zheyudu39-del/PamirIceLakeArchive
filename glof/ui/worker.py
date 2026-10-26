"""后台线程：把会联网 / 会读大文件的活儿搬出主线程。

分层硬约束（ui/__init__.py）：长任务禁止在主线程同步跑。
``build_series()`` 真的联网读 COG，年际一跑要几分钟；``load_scene() +
extract_lakes()`` 读一景也要秒级。这两个 worker 把活儿放进 ``QThread.run()``，
结果用信号送回主线程；任何异常都转成 ``failed(str)`` 过线——
线程边界上不能抛 Python 异常（没人能接住），更不能静默吞掉。

两个 worker 各自对应一个长任务：

    ExtractWorker   本地影像 -> NDWI -> 湖泊提取（单景，秒级）
    SeriesWorker    逐年联网检索 -> 面积序列（分钟级，必须可取消）

--------------------------------------------------------------------------
SeriesWorker.cancel() 的诚实语义（不要当成"即时取消"用）
--------------------------------------------------------------------------
``build_series()`` 的 progress 回调**只在失败事件上触发**
（检索失败 / 无可用影像 / 整日读取失败，见 core/timeseries.py 的 emit），
正常读数期间没有任何回调点。所以取消的实现方式是：

    cancel() 置位标记 -> progress 回调一进入就抛私有 _Cancelled 中断

由此可以推出它的真实行为，界面上必须照此理解：

    * 失败事件越多（网络差、年份多），取消响应越快；
    * 一切顺利、一个失败事件都没有时，取消**不会生效**，
      线程要等整条序列跑完才结束。

这不是 bug，是 build_series 提供给我们的全部回调间隙。本模块不假装
能做到即时取消；若将来 core 给正常观测也加了回调点，这里自然跟着变快。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from glof.core.indices import BAND_PRESETS, ndwi
from glof.core.io import Scene, load_scene
from glof.core.timeseries import SeriesPoint, build_series
from glof.core.water import LakeRecord, extract_lakes

__all__ = ["ExtractWorker", "SeriesWorker"]


class ExtractWorker(QThread):
    """单景提取：load_scene -> NDWI -> extract_lakes，全程工作线程内。

    Signals
    -------
    progressed(str)
        阶段文本（"读取影像 …" / "计算 NDWI" / …），界面拿去显示进度。
    extracted(object, list)
        ``(Scene, list[LakeRecord])``。Scene 连同波段栈一起交给主线程，
        地图模块直接用它出真彩色底图，不用再读一遍文件。
    failed(str)
        任何异常的可读文本。failed 之后不会再发 extracted。
    """

    progressed = Signal(str)
    extracted = Signal(object, list)
    failed = Signal(str)

    def __init__(
        self,
        input_path: str | Path,
        threshold: float = 0.15,
        min_area_km2: float = 0.01,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._input_path = str(input_path)
        self._threshold = float(threshold)
        self._min_area_km2 = float(min_area_km2)

    def run(self) -> None:
        try:
            self.progressed.emit(f"读取影像 {Path(self._input_path).name}")
            scene: Scene = load_scene(self._input_path, BAND_PRESETS["s2_l2a_6band"])

            self.progressed.emit("计算 NDWI")
            index = ndwi(scene["green"], scene["nir"])

            self.progressed.emit(
                f"提取湖泊（NDWI 阈值 {self._threshold}，最小 {self._min_area_km2} km²）"
            )
            records: list[LakeRecord] = extract_lakes(
                index,
                scene.transform,
                scene.crs,
                threshold=self._threshold,
                min_area_km2=self._min_area_km2,
            )

            self.progressed.emit(f"提取到 {len(records)} 个水体")
            self.extracted.emit(scene, records)
        except Exception as exc:  # noqa: BLE001 - 线程边界：异常必须转成信号过线
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _Cancelled(Exception):
    """cancel() 经 progress 回调抛出的私有中断，只在 worker 边界内存在。"""


class SeriesWorker(QThread):
    """时序检索：build_series() 的线程封装（真的联网，见模块 docstring）。

    Signals
    -------
    progressed(str)
        转发 build_series 的 progress 回调（只在失败事件上触发）。
    finished_pts(list)
        ``list[SeriesPoint]``，按日期升序；年份全部失败/无年份时是空 list。
    failed(str)
        任何异常的可读文本；取消也走这里，消息固定为"已取消"。
    """

    progressed = Signal(str)
    finished_pts = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        bbox,
        years,
        start_md: str = "06-01",
        end_md: str = "09-15",
        max_cloud: float = 60.0,
        per_year_limit: int = 60,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bbox = tuple(float(v) for v in bbox)
        self._years = [int(y) for y in years]
        self._start_md = str(start_md)
        self._end_md = str(end_md)
        self._max_cloud = float(max_cloud)
        self._per_year_limit = int(per_year_limit)
        self._cancelled = False

    def cancel(self) -> None:
        """请求取消。

        诚实语义（详见模块 docstring）：只置位标记，中断发生在下一次
        progress 回调进入时；build_series 正常读数期间没有回调点，
        所以取消可能在整条序列跑完之前都不生效。
        """
        self._cancelled = True

    def run(self) -> None:
        def on_progress(message: str) -> None:
            if self._cancelled:
                raise _Cancelled()
            self.progressed.emit(message)

        try:
            points: list[SeriesPoint] = build_series(
                self._bbox,
                self._years,
                start_md=self._start_md,
                end_md=self._end_md,
                max_cloud=self._max_cloud,
                per_year_limit=self._per_year_limit,
                progress=on_progress,
            )
        except _Cancelled:
            self.failed.emit("已取消")
            return
        except Exception as exc:  # noqa: BLE001 - 线程边界：异常必须转成信号过线
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished_pts.emit(list(points))
