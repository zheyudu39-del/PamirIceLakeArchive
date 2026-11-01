"""主窗口装配层：把地图 / 时序 / 表格三个模块拼成一个 QML 可用的应用状态。

为什么需要 AppState
--------------------
QML 侧（MapView / SeriesChart / LakeTable）只认一个上下文属性 ``app``，再经
``app.map`` / ``app.series`` / ``app.lakes`` 访问三个子模块。把"子模块怎么创建、
长任务怎么派发、结果怎么回流"集中在这一个类里，QML 的绑定路径恒定，各模块
自己不必关心线程与生命周期。

跨线程约定
----------
长任务在 ``worker.py`` 的 QThread 里跑，结果一律经信号回主线程（worker 信号 →
本类槽，跨线程 AutoConnection 自动排队）；QML 只读 NOTIFY 属性，从不接触
工作线程。worker 是临时对象，本类用"成员引用 + ``parent=self``"双保险持住：

* Python 侧没有引用的 QObject 会被 GC，QML / 信号连接就会踩到悬空对象；
* QThread 没跑完就析构是硬崩溃（"Destroyed while thread is still running"），
  busy 守卫保证不会在旧线程未结束时用新对象把它顶掉。

完成信号在本类再发一遍（``extractFinished`` / ``seriesFinished`` …）：worker
对装配层外部不可见，冒烟与宿主程序只能等 AppState 的生命周期信号，不该扒
内部线程的私有状态。

诚实性约定
----------
参数非法 / 文件不存在不静默，一律写进 ``lastError``，QML 横幅显示。
``extract`` 在默认影像（T43SGV 2025-09-30 小景）+ 契约参数 (0.15, 0.005) 下
提出 **0 条**记录是 core 的真实结果（该景 NDWI 最大约 0.167，实测见
scripts/smoke_ui_map.py / smoke_ui_series.py）——界面照常显示空表，不报错、
也不假装提出过湖。

风险接线（选中湖 → 时序 → 分级回流）
------------------------------------
时序结果统一从 :meth:`AppState._apply_series_result` 走（worker 回调与冒烟
注入共用同一条路径，Network 路径本身不变）：

1. 选中湖质心查 ICIMOD 事件库（``inventory.lake_profile``）拿坝型与已知
   溃决年——查不到（库里没有 / 库文件缺失）如实按 unknown / 空处理，不猜；
2. ``risk_series.assess_series`` 按坝型逐期判定（留出法政策在 core 一份）；
3. 取**面积最大的那一期**（不是最后一期——溃决后湖面会塌掉）的判定，
   经 ``LakeModel.update_risk``（表格 dataChanged）与
   ``MapController.update_lake_risk``（地图多边形重着色）回流界面；
4. 等级 + 判据值经 ``selectedLakeRisk`` 送到状态栏。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

from glof.core.inventory import DEFAULT_DB_PATH, lake_profile
from glof.core.risk import RiskAssessment
from glof.core.risk_series import assess_series
from glof.core.timeseries import SeriesPoint, estimate_plateau
from glof.core.water import LakeRecord
from glof.ui.lake_model import LakeModel
from glof.ui.map_controller import MapController
from glof.ui.series_controller import SeriesController
from glof.ui.worker import ExtractWorker, SeriesWorker

__all__ = ["AppState", "DEFAULT_INPUT", "PROJECT_ROOT", "PAD_DEG"]

#: 项目根目录（glof/ui/app_state.py 向上三级）。相对路径的影像若按 CWD 找不到，
#: 再按它找一遍——QML/快捷方式启动时 CWD 不一定是项目根。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 默认影像（契约素材）。作为命令行缺省值与 QML 输入框的初始文本。
DEFAULT_INPUT = "data/kyagar/S2A_MSIL2A_20250930T054251_R005_T43SGV_20250930T083759.tif"

#: 事件年兜底：仅当"选中了湖、但事件库文件不在位"（离线部署缺 data/raw，
#: ``lake_profile`` 返回 None）时用作曲线参考线。溃决年不再硬编码——正常
#: 路径一律查 ICIMOD 库（见 _apply_series_result），这里只是库缺失时的
#: 降级：参考线退回契约事件年（克亚吉尔 2018），风险判定仍按 unknown 走。
EVENT_YEARS = (2018,)

#: 选中湖时序检索窗口的半宽（度）。与 scripts/kyagar_series.py 的 PAD_DEG
#: 同口径（0.03° 约 3.3 km）：太小会把堰塞湖截断，面积系统性偏小。
PAD_DEG = 0.03


def _risk_summary_text(assessment: RiskAssessment) -> str:
    """状态栏风险摘要：等级 + 第一条有数值的判据（名称 = 值，阈值）。"""
    crit = next(
        (c for c in assessment.criteria
         if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)
         and math.isfinite(float(c.value))),
        None,
    )
    if crit is None:
        return assessment.level.value
    return (f"{assessment.level.value} · {crit.name} "
            f"{float(crit.value):.4f}（阈值 {crit.threshold}）")


class AppState(QObject):
    """QML 上下文属性 ``app`` 的本体：三个子模块的装配与长任务派发。

    Q_PROPERTY
    ----------
    map / series / lakes : QObject
        三个子模块（CONSTANT：装配后对象恒定，内部状态经各自的 NOTIFY 刷新）。
    defaultInput : str
        命令行 ``--input`` 传入的初始影像路径（CONSTANT），QML 输入框据此取默认值。
    busyExtract / busySeries : bool
        对应长任务是否在跑（NOTIFY），QML 用来禁用按钮并转圈。
    progressText / lastError : str
        阶段进度与最近一次错误（NOTIFY），QML 横幅与状态栏显示。
    selectedLakeId : int
        当前选中湖的 lake_id，-1 表示无选中（NOTIFY）。
    selectedLakeLabel : str
        选中湖摘要（id/面积/质心经纬度），无选中为 ""（NOTIFY）。
    selectedLakeRisk : str
        选中湖的风险摘要"等级 · 判据名 判据值（阈值）"，来自面积最大期的
        判定；无选中或尚未判定为 ""（NOTIFY）。

    Slots
    -----
    extract(inputPath, threshold, minArea)
        起 ExtractWorker；完成 → map.set_scene + map.set_lakes + lakes.set_records。
        换景后旧选中作废（records 是新对象），选中状态清回 -1。
    buildSeries(bbox, year0, year1)
        起 SeriesWorker；``bbox`` 支持 ``[x0,y0,x1,y1]`` 列表或四个 real 两种形状
        （叠加 @Slot 注册了两个重载）。**有选中湖时检索窗口忽略面板 bbox**，
        改用湖心 ±PAD_DEG（0.03°，与 scripts/kyagar_series.py 同口径）；完成 →
        _apply_series_result：查事件库（坝型/溃决年）→ assess_series 逐期判定
        → 面积最大期回流表格与地图 → series.set_series(points,
        estimate_plateau(points), 事件年)。year1 < year0 时年份区间为空，
        worker 不联网、立即返回空序列（冒烟就靠这条离线验证装配）。
    selectLake(lakeId)
        地图高亮并**记住**该湖（id/质心/面积），-1 = 取消高亮并清除选中；
        表格侧的联动由 QML 装配层（Main.qml）完成。
    cancelSeries()
        请求取消正在跑的时序检索（无任务时空操作）。能否立即中断取决于
        build_series 的回调间隙，诚实语义见 SeriesWorker.cancel()。

    冒烟/测试注入口
    ---------------
    ``_apply_series_result(points)`` 是时序结果的统一入口：worker 完成回调
    与冒烟注入共用，.SeriesWorker 的网络路径不变。
    """

    # ---- AppState 自己的生命周期信号（worker 完成后在此转发） ----
    extractFinished = Signal(object, list)  # (Scene, list[LakeRecord])
    extractFailed = Signal(str)
    seriesFinished = Signal(list)  # list[SeriesPoint]
    seriesFailed = Signal(str)

    # ---- NOTIFY 属性的变更信号 ----
    busyExtractChanged = Signal()
    busySeriesChanged = Signal()
    progressTextChanged = Signal()
    lastErrorChanged = Signal()
    selectedLakeChanged = Signal()
    selectedLakeRiskChanged = Signal()

    def __init__(
        self,
        provider: Any,
        parent: QObject | None = None,
        default_input: str = DEFAULT_INPUT,
    ) -> None:
        super().__init__(parent)
        # 三个子模块互不知晓彼此，联动（表↔图↔曲线）全在本类与 QML 装配层完成
        self._map = MapController(provider, self)
        self._series = SeriesController(self)
        self._lakes = LakeModel(self)
        self._default_input = str(default_input)

        self._busy_extract = False
        self._busy_series = False
        self._progress_text = ""
        self._last_error = ""
        # 选中湖（selectLake 记住本体；buildSeries 检索窗口与风险判定都挂它）
        self._selected_lake: LakeRecord | None = None
        # 选中湖的风险摘要（_apply_series_result 的判定回流，状态栏显示）
        self._selected_risk = ""

        # 持引用防 GC（见类 docstring）；busy 守卫保证同一时刻最多一个 worker
        self._extract_worker: ExtractWorker | None = None
        self._series_worker: SeriesWorker | None = None

    # ------------------------------------------------------------------
    # Q_PROPERTY
    # ------------------------------------------------------------------

    def _get_map(self) -> QObject:
        return self._map

    def _get_series(self) -> QObject:
        return self._series

    def _get_lakes(self) -> QObject:
        return self._lakes

    def _get_default_input(self) -> str:
        return self._default_input

    def _get_busy_extract(self) -> bool:
        return self._busy_extract

    def _get_busy_series(self) -> bool:
        return self._busy_series

    def _get_progress_text(self) -> str:
        return self._progress_text

    def _get_last_error(self) -> str:
        return self._last_error

    def _get_selected_lake_id(self) -> int:
        return self._selected_lake.lake_id if self._selected_lake is not None else -1

    def _get_selected_lake_label(self) -> str:
        rec = self._selected_lake
        if rec is None:
            return ""
        return (f"#{rec.lake_id} · {rec.area_km2:.4f} km² · "
                f"{rec.centroid_lon:.4f}°E {rec.centroid_lat:.4f}°N")

    def _get_selected_lake_risk(self) -> str:
        return self._selected_risk

    map = Property(QObject, _get_map, constant=True)  # noqa: N815 (QML 命名)
    series = Property(QObject, _get_series, constant=True)
    lakes = Property(QObject, _get_lakes, constant=True)
    defaultInput = Property(str, _get_default_input, constant=True)
    busyExtract = Property(bool, _get_busy_extract, notify=busyExtractChanged)
    busySeries = Property(bool, _get_busy_series, notify=busySeriesChanged)
    progressText = Property(str, _get_progress_text, notify=progressTextChanged)
    lastError = Property(str, _get_last_error, notify=lastErrorChanged)
    selectedLakeId = Property(int, _get_selected_lake_id, notify=selectedLakeChanged)
    selectedLakeLabel = Property(str, _get_selected_lake_label, notify=selectedLakeChanged)
    selectedLakeRisk = Property(str, _get_selected_lake_risk, notify=selectedLakeRiskChanged)

    # ------------------------------------------------------------------
    # 槽：长任务派发
    # ------------------------------------------------------------------

    @Slot(str, float, float)
    def extract(self, input_path: str, threshold: float, min_area: float) -> None:
        """单景提取（本地文件）。参数非法或文件不存在写 lastError，不抛异常。"""
        if self._busy_extract:
            return  # 同一任务不并发；QML 侧按钮此时已被禁用
        if not (math.isfinite(float(threshold)) and math.isfinite(float(min_area))
                and float(min_area) > 0.0):
            self._set_last_error("NDWI 阈值 / 最小面积必须是数字，且最小面积 > 0。")
            return
        path = self._resolve_input_path(input_path)
        if path is None:
            self._set_last_error(f"影像不存在：{input_path}")
            return

        worker = ExtractWorker(str(path), threshold=float(threshold),
                               min_area_km2=float(min_area), parent=self)
        worker.progressed.connect(self._on_extract_progress)
        worker.extracted.connect(self._on_extracted)
        worker.failed.connect(self._on_extract_failed)
        self._extract_worker = worker
        self._set_last_error("")
        self._set_busy_extract(True)
        self._set_progress_text("启动提取 …")
        worker.start()

    @Slot("QVariantList", int, int)
    @Slot(float, float, float, float, int, int)
    def buildSeries(self, *args) -> None:
        """年际时序检索（真的联网）。两种调用形状见类 docstring 的 Slots 说明。"""
        if self._busy_series:
            return
        if len(args) == 3:
            bbox, year0, year1 = args
        elif len(args) == 6:
            bbox, year0, year1 = list(args[:4]), args[4], args[5]
        else:
            self._set_last_error(
                "buildSeries 参数形状不对：需要 (bbox, 年起, 年止) 或四个坐标加两个年份。")
            return
        try:
            bbox4 = [float(v) for v in bbox]
            y0, y1 = int(year0), int(year1)
        except (TypeError, ValueError):
            self._set_last_error("bbox / 年份必须是数字。")
            return
        if len(bbox4) != 4:
            self._set_last_error(f"bbox 需要 4 个值 (x0,y0,x1,y1)，收到 {len(bbox4)} 个。")
            return

        if self._selected_lake is not None:
            # 选中湖优先：检索窗口取湖心 ±PAD_DEG（0.03°，与
            # scripts/kyagar_series.py 的 PAD_DEG 同口径——太小会把堰塞湖
            # 截断，面积系统性偏小），面板 bbox 此时只是摆设。
            rec = self._selected_lake
            lat, lon = rec.centroid_lat, rec.centroid_lon
            bbox4 = [lon - PAD_DEG, lat - PAD_DEG, lon + PAD_DEG, lat + PAD_DEG]
            window_note = f"选中湖 #{rec.lake_id} 湖心 ±{PAD_DEG}°"
        else:
            window_note = "面板 bbox"

        years = list(range(y0, y1 + 1))  # y1 < y0 时为空区间：不联网，返回空序列
        worker = SeriesWorker(bbox4, years=years, parent=self)
        worker.progressed.connect(self._on_series_progress)
        worker.finished_pts.connect(self._on_series_finished)
        worker.failed.connect(self._on_series_failed)
        self._series_worker = worker
        self._set_last_error("")
        self._set_busy_series(True)
        self._set_progress_text(
            f"检索时序 {y0}–{y1} …（{window_note}，联网，分钟级）"
            if years else f"检索时序（空年份区间，{window_note}）…")
        worker.start()

    @Slot(int)
    def selectLake(self, lake_id: int) -> None:
        """选中某湖并**记住**它；-1 = 取消高亮并清除选中。

        记住的是 LakeRecord 本体（id/面积/质心经 selectedLakeLabel 暴露，
        质心供 buildSeries 定检索窗口、_apply_series_result 查事件库）。
        id 不在当前记录里（如换景后残留的 id）按清除处理，不猜。重复点
        同一个湖不改状态（摘要不闪）。表格侧联动由 QML 装配层完成。
        """
        lid = int(lake_id)
        self._map.highlight(lid)
        rec = None
        if lid >= 0:
            rec = next((r for r in self._map.lake_records if r.lake_id == lid), None)
        if rec is not self._selected_lake:
            self._selected_lake = rec
            self._selected_risk = ""  # 换选中：旧判据摘要作废，等下一次时序判定
            self.selectedLakeRiskChanged.emit()
        self.selectedLakeChanged.emit()

    @Slot()
    def cancelSeries(self) -> None:
        """请求取消正在跑的时序检索（QML"取消检索"按钮的入口）。

        诚实语义（见 SeriesWorker.cancel()）：只置位标记，真正中断发生在
        下一次 progress 回调进入时——build_series 正常读数期间没有回调点，
        取消可能要等整条序列跑完才生效。没有在跑的检索时是空操作
        （_series_worker 完成后引用仍留着，用 isRunning() 挡掉）。
        """
        if self._series_worker is not None and self._series_worker.isRunning():
            self._series_worker.cancel()
            self._set_progress_text("已请求取消，等待检索中断 …")

    # ------------------------------------------------------------------
    # worker 信号 → 主线程槽
    # ------------------------------------------------------------------

    def _on_extract_progress(self, text: str) -> None:
        self._set_progress_text(text)

    def _on_extracted(self, scene, records: list) -> None:
        # 先 scene 后 lakes：set_lakes 依赖 set_scene 记下的几何基准，顺序不能换。
        # records 为空列表也照常走完（该景提取不到水体是合法结果）。
        self._map.set_scene(scene)
        self._map.set_lakes(records)
        self._lakes.set_records(records)
        # 新景新记录：旧选中（连同它的风险摘要与地图高亮）一并作废——
        # LakeRecord 是新对象，旧 id 在新表里指不定指到谁。
        if self._selected_lake is not None:
            self._selected_lake = None
            self._selected_risk = ""
            self._map.highlight(-1)
            self.selectedLakeChanged.emit()
            self.selectedLakeRiskChanged.emit()
        self._set_busy_extract(False)
        self._set_progress_text(f"提取完成：{len(records)} 个水体")
        self.extractFinished.emit(scene, records)

    def _on_extract_failed(self, message: str) -> None:
        self._set_busy_extract(False)
        self._set_progress_text("提取失败")
        self._set_last_error(message)
        self.extractFailed.emit(message)

    def _on_series_progress(self, text: str) -> None:
        self._set_progress_text(text)

    def _on_series_finished(self, points: list) -> None:
        pts = list(points)
        plateau = self._apply_series_result(pts)
        self._set_busy_series(False)
        if math.isfinite(plateau):
            self._set_progress_text(f"时序完成：{len(pts)} 期，平台 {plateau:.4f} km²")
        else:
            self._set_progress_text(f"时序完成：{len(pts)} 期（无有效平台估计）")
        self.seriesFinished.emit(pts)

    def _apply_series_result(self, points: list[SeriesPoint]) -> float:
        """时序结果统一入口：worker 回调与冒烟注入共用（核心接线）。

        SeriesWorker 的网络路径不变，这里只管"拿到一串点之后"的事：

        a) 选中湖质心查 ICIMOD 事件库拿坝型与已知溃决年；profile 为 None
           （库里没有 / 库文件缺失）→ lake_type="unknown"、event_years=[]
           （如实 UNKNOWN，不猜坝型）。库文件缺失时的唯一例外：参考线退回
           EVENT_YEARS 兜底（常量注释写明），风险判定仍是 unknown。
        b) ``risk_series.assess_series`` 按坝型逐期判定（留出法政策在 core）。
        c) 取**面积最大的那一期**（不是最后一期——溃决后湖面会塌掉）的判定，
           经 LakeModel.update_risk（表格 dataChanged 重着色）与
           MapController.update_lake_risk（地图多边形重着色）回流界面。
        d) series.set_series(points, estimate_plateau(points), 事件年)，并把
           "等级 + 判据值"写进 selectedLakeRisk 给状态栏。

        返回平台面积，供调用方写进度文本（数值口径与原 _on_series_finished
        一致：全序列 estimate_plateau）。
        """
        pts = list(points)
        plateau = estimate_plateau(pts)

        # ---- a) 事件库画像：坝型 + 已知溃决年 ----
        rec = self._selected_lake
        profile = (lake_profile(rec.centroid_lat, rec.centroid_lon)
                   if rec is not None else None)
        if profile is not None:
            # 库的坝型是自然语言（"Ice dammed"），下划线归一后进 risk 别名表
            # （"ice_dammed"→ice，"Moraine dammed"→moraine），其余如实 unknown。
            lake_type = profile.lake_type.lower().replace(" ", "_")
            # 曲线只立"序列年份范围内的"已知溃决年：库会把 1880–2019 的几十个
            # 年份全给出来，全传给图表会变成一摞参考线；SeriesChart 本来也把
            # 线立在"当年首个观测点"上（见 SeriesChart.qml 的 _eventYearTime）。
            series_years = {p.year for p in pts}
            event_years = [y for y in profile.event_years if y in series_years]
        else:
            lake_type = "unknown"
            if rec is not None and not DEFAULT_DB_PATH.is_file():
                # 兜底：选中了湖但事件库不在位（离线部署缺 data/raw）。
                # 参考线退回契约事件年（克亚吉尔 2018）；风险判定仍走
                # unknown 分支——缺库不缺诚实。
                event_years = list(EVENT_YEARS)
            else:
                # 库在位但该湖无已知事件（或未选中湖）：event_years 如实为空。
                event_years = []

        # ---- b) 逐期判定 ----
        assessments = assess_series(pts, lake_type) if pts else []

        # ---- c) 面积最大期回流表格与地图 ----
        if rec is not None and assessments:
            best = max(assessments, key=lambda a: a.area_km2)
            level = best.assessment.level.value
            self._lakes.update_risk(rec.lake_id, level, best.assessment.reasons)
            self._map.update_lake_risk(rec.lake_id, level)
            self._selected_risk = _risk_summary_text(best.assessment)
        else:
            self._selected_risk = ""
        self.selectedLakeRiskChanged.emit()

        # ---- d) 曲线数据 ----
        self._series.set_series(pts, plateau, event_years=event_years)
        return plateau

    def _on_series_failed(self, message: str) -> None:
        self._set_busy_series(False)
        self._set_progress_text("时序检索失败")
        self._set_last_error(message)
        self.seriesFailed.emit(message)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_input_path(raw: str) -> Path | None:
        """解析影像路径：CWD 优先，找不到再按项目根（见 PROJECT_ROOT 注释）。"""
        text = str(raw).strip().strip('"')
        if not text:
            return None
        path = Path(text)
        if path.is_file():
            return path
        if not path.is_absolute():
            candidate = PROJECT_ROOT / path
            if candidate.is_file():
                return candidate
        return None

    def _set_busy_extract(self, value: bool) -> None:
        if self._busy_extract != value:
            self._busy_extract = value
            self.busyExtractChanged.emit()

    def _set_busy_series(self, value: bool) -> None:
        if self._busy_series != value:
            self._busy_series = value
            self.busySeriesChanged.emit()

    def _set_progress_text(self, text: str) -> None:
        if self._progress_text != text:
            self._progress_text = str(text)
            self.progressTextChanged.emit()

    def _set_last_error(self, text: str) -> None:
        if self._last_error != text:
            self._last_error = str(text)
            self.lastErrorChanged.emit()
