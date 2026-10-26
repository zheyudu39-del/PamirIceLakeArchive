"""地图控制器：QML 地图视口与 core 提取结果之间的桥梁。

职责边界——QML 只管"画"，这里管"算"：

* 影像：``set_scene()`` 时把 Scene 转成 QPixmap 塞进 provider，并记下
  仿射变换与 CRS——之后所有像素↔经纬度的换算都以这景为基准。
* 多边形：core 给的湖是 WGS84 GeoJSON，而地图是像素坐标。``set_lakes()``
  把外环经纬度 → pyproj 投影回 scene.crs → 仿射反算成像素，QML 拿到
  ``polygons`` 直接画，不需要懂任何地理知识。
* 交互：点选（shapely prepared 判定，比逐次 contains 快）、悬停报经纬度。

为什么多边形发给 QML 的是像素坐标而不是经纬度：QML 侧坐标系就是影像
坐标系（缩放/平移都在像素空间里做），在 Python 侧一次性换算完，QML 的
绘制代码保持纯几何，两条链路（影像/多边形）天然对齐。

分层约束：本模块只调用 ``glof.core`` 的公开接口（Scene 下标、LakeRecord
字段），几何换算用的 pyproj/shapely 是契约指定的第三方依赖，不碰 core 内部。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, Property, Signal, Slot
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from shapely.prepared import prep

from glof.ui.image_provider import RasterImageProvider
from glof.ui.style import risk_color

__all__ = ["MapController"]


class MapController(QObject):
    """地图视口的状态与交互逻辑，QML 通过上下文属性 ``app.map`` 访问。"""

    imageUrlChanged = Signal()
    polygonsChanged = Signal()
    imageWidthChanged = Signal()
    imageHeightChanged = Signal()

    # 点中湖 → lake_id；点空白 → -1（主窗口用它取消选中/清详情）
    lakeClicked = Signal(int)
    # 悬停位置（WGS84 经纬度）；光标移出图外时两个值都是 NaN
    mouseMoved = Signal(float, float)
    # 载入了新景。宽度/高度/URL 都可能和上一景相同（两张同尺寸的整幅
    # S2 瓦片），QML 复位视口必须挂这个而不是挂宽度/高度变化。
    sceneChanged = Signal()

    def __init__(self, provider: RasterImageProvider, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        # 当前景的几何基准：像素→投影（正向），投影→像素（反向），两条经纬度通道
        self._transform = None
        self._inv_transform = None
        self._to_wgs84: Transformer | None = None
        self._from_wgs84: Transformer | None = None
        self._width = 0
        self._height = 0
        self._image_url = ""
        # 发给 QML 的多边形列表，元素结构见 set_lakes()
        self._polygons: list[dict] = []
        # 点选用的 prepared 几何（像素坐标），与 _polygons 同序；小湖在后
        self._hit_geoms: list[tuple[int, object]] = []
        # 当前装载的 LakeRecord（set_lakes() 填充）。空态给 []：未装载时读
        # lake_records 应返回空列表而不是 AttributeError（公开只读视图的契约）。
        self._records: list = []
        self._highlighted_id = -1

    # ------------------------------------------------------------------
    # Q_PROPERTY（QML 读这四个；全部带 NOTIFY，QML 绑定才会自动刷新）
    # ------------------------------------------------------------------

    @Property(str, notify=imageUrlChanged)
    def imageUrl(self) -> str:
        return self._image_url

    @Property("QVariantList", notify=polygonsChanged)
    def polygons(self) -> list[dict]:
        return self._polygons

    @Property(int, notify=imageWidthChanged)
    def imageWidth(self) -> int:
        return self._width

    @Property(int, notify=imageHeightChanged)
    def imageHeight(self) -> int:
        return self._height

    # ------------------------------------------------------------------
    # 数据装载
    # ------------------------------------------------------------------

    def set_scene(self, scene) -> None:
        """载入一景影像：provider 换图、记几何基准、清空多边形、换缓存键。"""
        from glof.ui.imagery import scene_to_pixmap  # 局部导入避免无谓的模块级耦合

        self._provider.set_pixmap(scene_to_pixmap(scene))
        self._transform = scene.transform
        self._inv_transform = ~scene.transform
        crs = CRS.from_user_input(scene.crs)
        self._to_wgs84 = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
        self._from_wgs84 = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)

        h, w = scene.shape
        if w != self._width:
            self._width = w
            self.imageWidthChanged.emit()
        if h != self._height:
            self._height = h
            self.imageHeightChanged.emit()

        self._set_polygons_unlocked([])
        # URL 带版本号：QML 的图片缓存按 URL 判重，不换键就不会重取图
        self._image_url = (
            f"image://{RasterImageProvider.PROVIDER_ID}/{self._provider.bump()}"
        )
        self.imageUrlChanged.emit()
        # 每次换景必发（与宽度/高度是否变化无关），见信号声明处的注释
        self.sceneChanged.emit()

    def set_lakes(self, records) -> None:
        """装载湖泊多边形：WGS84 外环 → scene.crs → 像素坐标。

        每个元素：``{"lakeId": int, "level": str, "color": str(risk_color),
        "rings": [[x, y], ...], "highlighted": bool}``。
        """
        if self._from_wgs84 is None:
            raise RuntimeError("先 set_scene() 载入影像，再 set_lakes()。")
        polygons: list[dict] = []
        hits: list[tuple[int, object]] = []
        for rec in records:
            geom = shape(json.loads(rec.geometry_wgs84_json))
            # 展示只画外环；MultiPolygon（提取时多个碎片被并成一个记录）取
            # 面积最大的子多边形，避免把几个不相连的环用线段串起来。
            # 点选仍用完整几何（全部碎片）：点到任何碎片上都能选中这条记录。
            poly = (
                max(geom.geoms, key=lambda g: g.area)
                if geom.geom_type == "MultiPolygon"
                else geom
            )
            rings = []
            for lon, lat in poly.exterior.coords:
                x, y = self._from_wgs84.transform(lon, lat)
                px, py = self._inv_transform * (x, y)
                rings.append([float(px), float(py)])
            polygons.append(
                {
                    "lakeId": int(rec.lake_id),
                    "level": rec.risk_level,
                    "color": risk_color(rec.risk_level),
                    "rings": rings,
                    "highlighted": int(rec.lake_id) == self._highlighted_id,
                }
            )
            # 命中测试必须用**像素坐标**的完整几何（selectAt 收到的就是像素），
            # 这里把 WGS84 几何整体反算过去，拓扑不变。
            hits.append((int(rec.lake_id), prep(self._geom_to_px(geom))))
        self._records = list(records)
        self._set_polygons_unlocked(polygons, hits)

    def _geom_to_px(self, geom):
        """shapely 几何 WGS84 → 像素坐标（经纬度→投影→仿射反算，逐点）。"""
        from_wgs84, inv = self._from_wgs84, self._inv_transform

        def fn(xs, ys):
            px_list, py_list = [], []
            for lon, lat in zip(xs, ys):
                x, y = from_wgs84.transform(lon, lat)
                px, py = inv * (x, y)
                px_list.append(px)
                py_list.append(py)
            return px_list, py_list

        return shp_transform(fn, geom)

    def highlight(self, lake_id: int) -> None:
        """高亮某个湖（-1 表示全部取消），重发 polygons 让 QML 重画。"""
        self._highlighted_id = int(lake_id)
        if not self._polygons:
            return
        for p in self._polygons:
            p["highlighted"] = p["lakeId"] == self._highlighted_id
        self.polygonsChanged.emit()

    def _set_polygons_unlocked(
        self,
        polygons: list[dict],
        hits: list[tuple[int, object]] | None = None,
    ) -> None:
        self._polygons = polygons
        self._hit_geoms = hits if hits is not None else []
        self.polygonsChanged.emit()

    # ------------------------------------------------------------------
    # QML 调用的交互槽（像素坐标 = QML 侧缩放前的影像坐标）
    # ------------------------------------------------------------------

    @Slot(int, int)
    def selectAt(self, px: int, py: int) -> None:  # noqa: N802 (Qt 命名)
        """点选：命中哪个湖发哪个 id；点空白发 -1。

        records 按面积降序排，倒序遍历让小湖优先命中——相邻/相切时
        点到大湖边缘的缺口不会误选大湖。
        """
        if not self._hit_geoms:
            self.lakeClicked.emit(-1)
            return
        point = Point(float(px), float(py))
        for lake_id, prep_geom in reversed(self._hit_geoms):
            if prep_geom.intersects(point):
                self.lakeClicked.emit(int(lake_id))
                return
        self.lakeClicked.emit(-1)

    @Slot(float, float)
    def hoverAt(self, px: float, py: float) -> None:  # noqa: N802 (Qt 命名)
        """悬停：像素 → 投影 → WGS84 经纬度。图外（或未载景）发 NaN。"""
        if self._transform is None or not (
            0.0 <= px < self._width and 0.0 <= py < self._height
        ):
            self.mouseMoved.emit(float("nan"), float("nan"))
            return
        x, y = self._transform * (float(px), float(py))
        lon, lat = self._to_wgs84.transform(x, y)
        self.mouseMoved.emit(float(lon), float(lat))

    def lake_bbox_px(self, lake_id: int) -> tuple[float, float, float, float] | None:
        """湖外环的像素包围盒 ``(x0, y0, x1, y1)``，供 QML ``zoomTo`` 定位；找不到返回 None。"""
        for p in self._polygons:
            if p["lakeId"] == int(lake_id):
                xs = [pt[0] for pt in p["rings"]]
                ys = [pt[1] for pt in p["rings"]]
                return (min(xs), min(ys), max(xs), max(ys))
        return None

    @property
    def lake_records(self) -> list:
        """当前装载的 LakeRecord 列表（只读视图，供主窗口联动详情/表格）。"""
        return list(self._records)
