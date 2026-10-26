// 面积时序曲线（QtCharts 实现）。
//
// 数据来源：上下文属性 app.series（series_controller.SeriesController），
// 本文件不 import Python、不认识 SeriesPoint——只认 controller 的五个
// Q_PROPERTY（points / plateau / eventYears / highlighted / palette）。
//
// 颜色约定：语义色全部取自 app.series.palette（由 style.py 的 RISK_COLORS
// 注入——平台线橙、事件年红、高亮红、空态文字灰）。主曲线颜色不在此
// 指定，交给 ChartView 主题的默认系列色；本图没有风险等级元素，因此
// 不引用 RISK_COLORS 之外也不自造任何色值。
//
// 与冒烟/联调的接口：根节点暴露只读 shownPoints / shownHighlights，
// 以及可调用的 pixelOfTime() / plotAreaRect() / pickNearest()——
// 后者就是 MouseArea 点击走的同一条"就近取点"路径，测试可以直接调。
//
// 为什么没有 Canvas 退化路径：QtCharts 在 offscreen+software 下的可用性
// 已在最前头用最小 QML 验证过（见 scripts/smoke_ui_series.py 步骤 1；
// 注意它必须配 QApplication，QGuiApplication 下 ChartView 构造即崩）。
// 验证通过后才定的 QtCharts 方案，所以不养一条跑不到的死代码。

import QtQuick
import QtCharts

Item {
    id: root

    implicitWidth: 640
    implicitHeight: 420

    // ---- 对外只读状态（冒烟断言 / 主窗口联调用）----
    property int shownPoints: mainSeries.count
    property int shownHighlights: highlightSeries.count
    property bool hasData: seriesPoints.length > 0

    // ---- controller 数据的本地快照 ----
    // 绑定表达式里反复跨 Q_PROPERTY 取值既慢又难调试；统一落地成快照，
    // 重绘只读快照。controller 的四个变更信号都会触发重新同步。
    property var seriesPoints: []
    property var seriesEventYears: []
    property real seriesPlateau: NaN
    property string seriesHighlighted: ""

    readonly property var pal: app.series.palette

    // 事件年的动态 LineSeries 与标注定位信息，刷新前整体清掉
    property var _eventSeries: []
    property var _eventMarks: []
    property var _labels: []

    function _sync() {
        var ctrl = app.series
        seriesPoints = ctrl.points
        seriesPlateau = ctrl.plateau
        seriesEventYears = ctrl.eventYears
        seriesHighlighted = ctrl.highlighted
        _refresh()
    }

    Component.onCompleted: _sync()

    Connections {
        target: app.series
        function onPointsChanged() { root._sync() }
        function onPlateauChanged() { root._sync() }
        function onEventYearsChanged() { root._sync() }
        function onHighlightedChanged() { root._sync() }
    }

    // ---- 坐标换算（数据域 <-> plotArea 像素域）----
    // DateTimeAxis/ValuesAxis 的 min/max 就是绘图区两端，线性映射即精确解。

    function pixelOfTime(t) {
        var pa = chart.plotArea
        var t0 = axisX.min.getTime()
        var t1 = axisX.max.getTime()
        if (t1 <= t0)
            return pa.x
        return pa.x + (t - t0) / (t1 - t0) * pa.width
    }

    function valueToPixel(v) {
        var pa = chart.plotArea
        var v0 = axisY.min
        var v1 = axisY.max
        if (v1 <= v0)
            return pa.y + pa.height
        return pa.y + pa.height - (v - v0) / (v1 - v0) * pa.height
    }

    function plotAreaRect() {
        return chart.plotArea
    }

    // ---- 就近取点（MouseArea 点击与测试共用的同一条路径）----

    function pickNearest(px, py) {
        var pa = chart.plotArea
        // plotArea 是 QRectF 不是 Item，锚不了；MouseArea 填满 ChartView
        // 之后在这里做命中判断，效果等同"覆盖 plotArea"
        if (px < pa.x || px > pa.x + pa.width || py < pa.y || py > pa.y + pa.height)
            return
        var pts = seriesPoints
        if (!pts.length)
            return
        var best = -1
        var bestDist = Infinity
        for (var i = 0; i < pts.length; i++) {
            var t = Date.parse(pts[i].date)
            if (isNaN(t))
                continue
            var d = Math.abs(pixelOfTime(t) - px)
            if (d < bestDist) {
                bestDist = d
                best = i
            }
        }
        if (best >= 0)
            app.series.chartClicked(pts[best].date)
    }

    // ---- 渲染 ----

    function _refresh() {
        mainSeries.clear()
        plateauSeries.clear()
        highlightSeries.clear()
        _clearEventLines()
        _clearLabels()

        var pts = seriesPoints
        var t0 = NaN
        var t1 = NaN
        var yMax = 0
        for (var i = 0; i < pts.length; i++) {
            var t = Date.parse(pts[i].date)
            if (isNaN(t))
                continue
            mainSeries.append(t, pts[i].areaKm2)
            if (isNaN(t0))
                t0 = t
            t1 = t
            if (pts[i].areaKm2 > yMax)
                yMax = pts[i].areaKm2
        }

        if (!isNaN(t0)) {
            // x：数据范围两侧各留 5%（不足补 3 天），首尾点不贴边
            var pad = Math.max((t1 - t0) * 0.05, 3 * 24 * 3600 * 1000)
            axisX.min = new Date(t0 - pad)
            axisX.max = new Date(t1 + pad)
            // y：0 起步，顶端留 12% 给平台标注文字
            var top = Math.max(yMax, isNaN(seriesPlateau) ? 0 : seriesPlateau)
            axisY.max = top > 0 ? top * 1.12 : 1

            if (!isNaN(seriesPlateau)) {
                plateauSeries.append(axisX.min.getTime(), seriesPlateau)
                plateauSeries.append(axisX.max.getTime(), seriesPlateau)
            }

            for (var k = 0; k < seriesEventYears.length; k++)
                _addEventLine(seriesEventYears[k])

            if (seriesHighlighted !== "")
                _addHighlight()
        }

        // plotArea 要等布局完成才准确：先 callLater 兜底，
        // 之后每次 plotAreaChanged 也会重摆（_placeLabels 自己先清旧）
        Qt.callLater(root._placeLabels)
    }

    function _eventYearTime(year) {
        // 立在"这一年的第一个观测点"上，多图多年时天然落在数据块内；
        // 该年无数据时退回 1 月 1 日。日期统一按 UTC（ISO 串解析即 UTC 零点）
        var pts = seriesPoints
        for (var i = 0; i < pts.length; i++) {
            var t = Date.parse(pts[i].date)
            if (!isNaN(t) && new Date(t).getUTCFullYear() === year)
                return t
        }
        return Date.parse(year + "-01-01")
    }

    function _addEventLine(year) {
        var t = _eventYearTime(year)
        if (isNaN(t))
            return
        var t0 = axisX.min.getTime()
        var t1 = axisX.max.getTime()
        if (t1 <= t0)
            return
        var span = t1 - t0
        // 收进轴范围两端各 2% 之内，保证竖线永远画在图内
        t = Math.min(Math.max(t, t0 + span * 0.02), t1 - span * 0.02)
        var s = chart.createSeries(ChartView.SeriesTypeLine, "eventYear" + year, axisX, axisY)
        s.style = Qt.DotLine
        s.width = 1.5
        s.color = pal.event
        s.pointsVisible = false
        s.append(t, axisY.min)
        s.append(t, axisY.max)
        _eventSeries.push(s)
        _eventMarks.push({ year: year, t: t })
    }

    function _clearEventLines() {
        for (var i = 0; i < _eventSeries.length; i++)
            chart.removeSeries(_eventSeries[i])
        _eventSeries = []
        _eventMarks = []
    }

    function _addHighlight() {
        var pts = seriesPoints
        for (var i = 0; i < pts.length; i++) {
            if (pts[i].date === seriesHighlighted) {
                var t = Date.parse(pts[i].date)
                if (!isNaN(t))
                    highlightSeries.append(t, pts[i].areaKm2)
                return
            }
        }
        // 高亮日期不在当前序列里（旧序列的高亮残留）——不画，不猜
    }

    function _clearLabels() {
        for (var i = 0; i < _labels.length; i++)
            _labels[i].destroy()
        _labels = []
    }

    function _placeLabels() {
        _clearLabels()
        var pa = chart.plotArea
        if (!hasData || pa.width <= 0 || pa.height <= 0)
            return

        // 平台标注：虚线右上方；空间不够就挪到线下
        if (!isNaN(seriesPlateau)) {
            var pl = plateauLabelComp.createObject(overlay)
            pl.x = pa.x + pa.width - pl.implicitWidth - 6
            pl.y = valueToPixel(seriesPlateau) - pl.implicitHeight - 3
            if (pl.y < pa.y + 2)
                pl.y = valueToPixel(seriesPlateau) + 3
            _labels.push(pl)
        }

        // 事件年标注：竖线顶端右侧
        for (var i = 0; i < _eventMarks.length; i++) {
            var yl = yearLabelComp.createObject(overlay)
            yl.text = String(_eventMarks[i].year)
            yl.x = pixelOfTime(_eventMarks[i].t) + 4
            yl.y = pa.y + 2
            _labels.push(yl)
        }
    }

    Connections {
        target: chart
        function onPlotAreaChanged() { root._placeLabels() }
    }

    // ---- 图元 ----

    ChartView {
        id: chart
        anchors.fill: parent
        theme: ChartView.ChartThemeLight
        animationOptions: ChartView.NoAnimation
        antialiasing: true
        legend.visible: false

        LineSeries {
            id: mainSeries
            axisX: DateTimeAxis {
                id: axisX
                format: "yyyy-MM"
            }
            axisY: ValuesAxis {
                id: axisY
                titleText: "面积 km²"
                min: 0
                max: 1
                labelFormat: "%.1f"
            }
            pointsVisible: true       // 圆点 marker
            width: 2
        }

        LineSeries {
            id: plateauSeries
            axisX: axisX
            axisY: axisY
            style: Qt.DashLine
            width: 2
            color: root.pal.plateau
        }

        ScatterSeries {
            id: highlightSeries
            axisX: axisX
            axisY: axisY
            markerSize: 14
            markerShape: ScatterSeries.MarkerShapeCircle
            color: root.pal.highlight
            borderColor: root.pal.highlight
        }

        MouseArea {
            anchors.fill: parent
            enabled: root.hasData
            onClicked: function (mouse) { root.pickNearest(mouse.x, mouse.y) }
        }
    }

    // 标注层：chart 的兄弟节点叠在上面；Text 不吃鼠标事件，不影响点击
    Item {
        id: overlay
        anchors.fill: parent
    }

    Text {
        anchors.centerIn: parent
        visible: !root.hasData
        text: "检索时序后显示曲线"
        font.pixelSize: 14
        color: root.pal.muted
    }

    Component {
        id: yearLabelComp
        Text {
            font.pixelSize: 11
            font.bold: true
            color: root.pal.event
        }
    }

    Component {
        id: plateauLabelComp
        Text {
            font.pixelSize: 11
            color: root.pal.plateau
            text: "满蓄平台 "
                  + (Number.isNaN(root.seriesPlateau) ? "—" : root.seriesPlateau.toFixed(3))
                  + " km²"
        }
    }
}
