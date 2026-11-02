// 地图视口：影像 + 风险多边形叠加 + 点选/悬停/缩放平移。
//
// 结构（与主窗口的集成契约）：
//   Flickable（contentWidth/Height = 影像尺寸 × zoom）
//     └ Item（scale: zoom，transformOrigin: TopLeft）——内部坐标恒为影像像素
//         ├ Image        真彩色影像（image://raster/full?v=N）
//         ├ Repeater     每个湖一个 Shape，画风险多边形
//         └ MouseArea    点选/悬停/滚轮，坐标直接就是影像像素
//
// 坐标系约定：本组件内部一切坐标都是影像像素；经纬度换算全在
// MapController（app.map）里做，QML 侧不出现任何地理概念。
//
// 已实测的渲染坑（PySide6 6.12 / QT_QUICK_BACKEND=software）：
// 1. ShapePath 里不能放 Repeater（pathElements 只收 QQuickPathElement），
//    所以"每个湖一个 Shape（Repeater 的 Item delegate）+ 环内用
//    PathPolyline"——PathPolyline 就是把一串 PathMove/PathLine 打包的
//    路径元素，闭合环由控制器给的 rings 首尾重复保证。
// 2. PathPolyline 的 path 只认 Qt.point() 点列表，{x,y} 字面量会静默画空。
// 3. 默认 GeometryRenderer 在软件后端下整层不渲染，但多 Shape 每个只画
//    一个多边形，实测正常（见 scripts/smoke_ui_map.py 截图）。

import QtQuick
import QtQuick.Shapes
import "."

Rectangle {
    id: mapRoot
    objectName: "mapView"

    color: "#111418"

    // 圆角裁剪：Flickable 里的 Image 会画到父级圆角之外，不加 clip
    // 左上角会是直角（实测）。clip 对圆角矩形同样生效。
    radius: 7
    clip: true

    // ---- 来自 MapController 的状态（全 NOTIFY，绑定自动刷新） ----
    readonly property int imageWidth: app.map ? app.map.imageWidth : 0
    readonly property int imageHeight: app.map ? app.map.imageHeight : 0

    // fit 倍率：把整景"铺满"视口（cover）。短边方向可能超出视口交给拖动，
    // 这是地图类应用的常规初始态。
    readonly property real fitZoom:
        (imageWidth > 0 && flick.width > 0 && flick.height > 0)
        ? Math.max(flick.width / imageWidth, flick.height / imageHeight)
        : 1.0

    // 当前倍率。绑定到 fitZoom = 初始即 fit；用户缩放/zoomTo 赋值后绑定断开，
    // 换景时底部 onSceneChanged 里的 fit() 会显式恢复。
    property real zoom: fitZoom

    readonly property real maxZoom: 4.0

    // 影像是否已从 provider 就绪（冒烟与主窗口都可读；Image.status 的
    // 枚举从 Python 读会被 PySide6 拒绝转换，所以在这里包成 bool）
    readonly property bool imageReady: mapImage.status === Image.Ready

    // ---- 对外暴露：定位到某个湖的像素包围盒（供主窗口联动表格/详情） ----
    function zoomTo(x0, y0, x1, y1) {
        if (imageWidth <= 0)
            return;
        var pad = Math.max(12, Math.min(flick.width, flick.height) * 0.06);
        var zw = flick.width / Math.max(1, (x1 - x0) + 2 * pad);
        var zh = flick.height / Math.max(1, (y1 - y0) + 2 * pad);
        zoom = clampZoom(Math.min(zw, zh));
        // 包围盒中心对准视口中心，再夹进内容范围
        flick.contentX = clampRange((x0 + x1) / 2 * zoom - flick.width / 2,
                                    0, Math.max(0, flick.contentWidth - flick.width));
        flick.contentY = clampRange((y0 + y1) / 2 * zoom - flick.height / 2,
                                    0, Math.max(0, flick.contentHeight - flick.height));
    }

    // 恢复初始 fit 并居中（换景 / 需要复位时调用）
    function fit() {
        if (imageWidth <= 0)
            return;
        zoom = fitZoom;
        flick.contentX = clampRange((flick.contentWidth - flick.width) / 2,
                                    0, Math.max(0, flick.contentWidth - flick.width));
        flick.contentY = clampRange((flick.contentHeight - flick.height) / 2,
                                    0, Math.max(0, flick.contentHeight - flick.height));
    }

    // 以影像像素 (px, py) 为锚缩放 factor 倍：锚点在视口中的位置保持不动。
    // 推导：锚点视口坐标 = px*zoom - contentX，缩放后令其不变，
    // 得 contentX' = contentX + px*(zoom' - zoom)。
    function zoomAt(factor, px, py) {
        if (imageWidth <= 0)
            return;
        var nz = clampZoom(zoom * factor);
        if (Math.abs(nz - zoom) < 1e-9)
            return;
        var ncx = flick.contentX + px * (nz - zoom);
        var ncy = flick.contentY + py * (nz - zoom);
        zoom = nz;
        flick.contentX = clampRange(ncx, 0, Math.max(0, flick.contentWidth - flick.width));
        flick.contentY = clampRange(ncy, 0, Math.max(0, flick.contentHeight - flick.height));
    }

    function clampZoom(v) {
        return Math.min(maxZoom, Math.max(fitZoom, v));
    }

    function clampRange(v, lo, hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    // 换景复位挂 sceneChanged（set_scene 必发）：宽度可能两次都相同（如两张
    // 同为 10980 宽的整幅 S2 瓦片），挂 imageWidthChanged 会漏掉这种换景，
    // 旧景的缩放/平移就被原样带进新景。控制器在宽度/高度更新完之后才发，
    // fit() 读到的必是新景尺寸。
    Connections {
        target: app.map
        function onSceneChanged() { fit() }
    }
    Component.onCompleted: fit()

    Flickable {
        id: flick
        objectName: "mapFlick"
        anchors.fill: parent
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: imageWidth > 0

        contentWidth: imageWidth * mapRoot.zoom
        contentHeight: imageHeight * mapRoot.zoom

        Item {
            id: content
            width: imageWidth
            height: imageHeight
            scale: mapRoot.zoom
            transformOrigin: Item.TopLeft

            Image {
                id: mapImage
                objectName: "mapImage"
                anchors.fill: parent
                source: app.map ? app.map.imageUrl : ""
                asynchronous: true
                fillMode: Image.Stretch
                visible: imageWidth > 0
            }

            // 每个湖一个 Shape：描边 1px（除以 zoom 抵消缩放 = 化妆笔），
            // 高亮的加粗 + 半透明填充。颜色由控制器按 risk_color 给好。
            Repeater {
                objectName: "polygonsRepeater"
                model: app.map ? app.map.polygons : []

                delegate: Shape {
                    anchors.fill: parent
                    antialiasing: true

                    ShapePath {
                        strokeColor: modelData.color
                        strokeWidth: modelData.highlighted ? 3.0 / mapRoot.zoom
                                                           : 1.0 / mapRoot.zoom
                        fillColor: modelData.highlighted ? Qt.alpha(modelData.color, 0.35)
                                                         : "transparent"
                        PathPolyline {
                            path: modelData.rings.map(function (p) {
                                return Qt.point(p[0], p[1]);
                            })
                        }
                    }
                }
            }

            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.LeftButton

                // mouse.x/y 已被映射进本坐标系 = 影像像素
                onClicked: (mouse) => app.map.selectAt(Math.round(mouse.x), Math.round(mouse.y))
                onPositionChanged: (mouse) => app.map.hoverAt(mouse.x, mouse.y)
                onWheel: (wheel) => {
                    mapRoot.zoomAt(wheel.angleDelta.y > 0 ? 1.25 : 1 / 1.25,
                                   wheel.x, wheel.y)
                }
            }
        }
    }

    // 空态不是故障态：把下一步操作直接写在这里。
    Column {
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 390)
        spacing: 12
        visible: imageWidth === 0

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "影像视图"
            color: "#e5e9ef"
            font.pixelSize: 20
            font.bold: true
        }
        Text {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            text: "选择本地 Sentinel-2 影像并点击「开始提取」，\n或点击「刷新最新观测」联网获取影像。"
            color: "#b2bbc6"
            font.pixelSize: Theme.fontLabel
            lineHeight: 1.4
        }
        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: hintRow.width + 24
            height: 28
            radius: 14
            color: "#242b34"
            border.color: "#39434f"

            Row {
                id: hintRow
                anchors.centerIn: parent
                spacing: 8
                Text { text: "滚轮缩放"; color: "#c0c8d2"; font.pixelSize: 11 }
                Text { text: "·"; color: "#6f7b88"; font.pixelSize: 11 }
                Text { text: "拖动平移"; color: "#c0c8d2"; font.pixelSize: 11 }
                Text { text: "·"; color: "#6f7b88"; font.pixelSize: 11 }
                Text { text: "点击湖泊查看判据"; color: "#c0c8d2"; font.pixelSize: 11 }
            }
        }
    }
}
