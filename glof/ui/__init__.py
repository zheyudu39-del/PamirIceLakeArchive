"""界面层（PySide6）。

分层硬约束（docs §2.3 / README）：

- 本层只允许调用 ``glof.core`` 的公开接口；core 不 import 本层、不 import 任何 Qt。
- 长任务（联网检索、大图读取）必须走 ``worker`` 的 QThread，禁止阻塞主线程。

模块划分：

    imagery            栅格 → QImage/QPixmap 的显示转换（拉伸 / stride 对齐）
    image_provider     QML Image 的图片提供器（image://raster/...，换景用 bump 换键）
    map_controller     地图视口状态与交互（影像装载 / 多边形投影到像素 / 点选悬停）
    worker             QThread 后台任务（单景提取 / 联网时序检索）
    series_controller  面积时序曲线的视图模型（QVariantList + palette）
    lake_model         湖泊/风险列表的 QAbstractTableModel（角色 lakeId/color/...）
    app_state          主窗口装配：Q_PROPERTY map/series/lakes + 长任务派发与回流
    qml/               QML 组件：MapView / SeriesChart / LakeTable / Main

装配结构（app_state.AppState 作为 QML 上下文属性 ``app``）::

    app.map      → map_controller.MapController（依赖注入 RasterImageProvider）
    app.series   → series_controller.SeriesController
    app.lakes    → lake_model.LakeModel

长任务在 ``worker`` 的 QThread 里跑，结果经信号回主线程：extract 完成后由
AppState 写入 map（set_scene + set_lakes）与 lakes（set_records）；buildSeries
完成后写入 series（set_series + estimate_plateau）。联动（表↔图↔曲线）在
QML 装配层（qml/Main.qml）与 AppState.selectLake 完成。

入口：``python -m glof.ui [--input PATH] [--smoke]``（见 ``__main__.py``：
QApplication + QQmlApplicationEngine，``engine.addImageProvider("raster", ...)``
注入地图影像，--smoke 为无头全链路冒烟）。
"""
