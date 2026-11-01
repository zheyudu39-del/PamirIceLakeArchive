// 主窗口装配：左参数面板 + 中间地图 + 底部"面积时序 / 湖泊列表"切换 + 状态栏。
//
// 装配契约（与 glof/ui/app_state.py 对齐）：
//   * 上下文属性 app（AppState）：app.map / app.series / app.lakes 三个子对象，
//     busyExtract / busySeries / progressText / lastError / selectedLakeId /
//     selectedLakeLabel / selectedLakeRisk 全 NOTIFY 属性；
//     槽 extract(inputPath, threshold, minArea)、
//        buildSeries([x0,y0,x1,y1], year0, year1)（也支持四个 real 的形状；
//        有选中湖时检索窗口用湖心 ±0.03°，见 app_state.PAD_DEG）、
//        cancelSeries()（请求取消时序检索）、selectLake(lakeId)（记住选中湖，
//        时序判定回流经 selectedLakeRisk 显示在状态栏）、
//        refreshLatest([x0,y0,x1,y1], lookbackDays)（联网取最近一景，结果经
//        latestDate / latestAgeDays / latestAreaKm2 / latestNLakes /
//        latestMessage / latestBusy 显示；面积没量到时 latestAreaKm2 是 NaN，
//        界面写"面积没量到（无水面信号）"，绝不显示 0.00 km²）。
//   * 联动回环的防法（与 LakeTable.qml 文件头同一条注释）：
//       表格点行 → table.lakeClicked → app.selectLake → 地图高亮；
//       地图点湖 → app.map.lakeClicked → 这里调 table.selectRow（程序化选中
//       **不发** lakeClicked，信号不会转回地图，不成环）。
//       两个方向共用同一条视觉结果，断环的关键在 LakeTable.selectRow 的静默语义。
//
// 为什么底部用 TabBar + StackLayout 而不是 TabView：TabBar+StackLayout 在
// QtQuick.Controls 全 6.x 可用、行为一致；且 StackLayout 的两个孩子从加载起
// 就实例化——地图↔表格的联动不依赖用户先切过页签。
//
// 参数默认值与 data/kyagar 契约素材一致。诚实提示：该景（2025-09-30 小景）
// NDWI 最高约 0.167，阈值 0.15 下提出 0 条是正常结果，面板上写明而不是让
// 用户以为程序坏了。

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    objectName: "mainWindow"
    width: 1360
    height: 840
    minimumWidth: 1100
    minimumHeight: 700
    visible: true
    title: "帕米尔冰湖档案"
    color: "#f1f3f4"

    // ---- 状态栏文本（Connections 里回填，见文件底部） ----
    property string coordText: "悬停：—"
    property string pointText: "点选：—"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 8

        // ---- progressText / lastError 横幅：有错误显示错误（红底），否则显示进度 ----
        Rectangle {
            objectName: "banner"
            Layout.fillWidth: true
            implicitHeight: bannerLabel.implicitHeight + 16
            radius: 4
            visible: app.lastError !== "" || app.progressText !== ""
            color: app.lastError !== "" ? "#fdecea" : "#e8f0fe"

            Text {
                id: bannerLabel
                anchors.fill: parent
                anchors.margins: 8
                verticalAlignment: Text.AlignVCenter
                wrapMode: Text.Wrap
                text: app.lastError !== "" ? app.lastError : app.progressText
                color: app.lastError !== "" ? "#b3261e" : "#1a56b0"
                font.pixelSize: 13
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 8

            // ---- 左：参数面板 ----
            Rectangle {
                Layout.preferredWidth: 330
                Layout.fillHeight: true
                color: "white"
                radius: 6

                // 面板内容比窗口高时（最小窗口 1100×700 实测）必须能滚：
                // 直接 ColumnLayout 会把"最新观测"整段裁在可视区外，
                // 用户以为按钮不存在。Flickable 不影响任何 objectName/绑定。
                Flickable {
                    id: panelFlick
                    objectName: "panelFlick"
                    anchors.fill: parent
                    anchors.margins: 12
                    contentWidth: width
                    contentHeight: Math.max(panelColumn.implicitHeight, height)
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                    ColumnLayout {
                        id: panelColumn
                        // 右侧留 12px 给滚动条；内容不足一屏时仍撑满，
                        // 底部的弹性占位继续把提示顶到面板底部（原行为）。
                        width: panelFlick.width - 12
                        height: Math.max(implicitHeight, panelFlick.height)
                        spacing: 8

                    Label {
                        text: "影像提取"
                        font.bold: true
                        font.pixelSize: 14
                        color: "#202124"
                    }

                    Label { text: "影像路径"; font.pixelSize: 12; color: "#5f6368" }
                    TextField {
                        id: inputField
                        objectName: "inputField"
                        Layout.fillWidth: true
                        text: app.defaultInput
                        selectByMouse: true
                        font.pixelSize: 12
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: "NDWI 阈值"
                            font.pixelSize: 12
                            color: "#5f6368"
                            Layout.fillWidth: true
                        }
                        TextField {
                            id: thresholdField
                            objectName: "thresholdField"
                            text: "0.15"
                            Layout.preferredWidth: 80
                            selectByMouse: true
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: "最小面积 km²"
                            font.pixelSize: 12
                            color: "#5f6368"
                            Layout.fillWidth: true
                        }
                        TextField {
                            id: minAreaField
                            objectName: "minAreaField"
                            text: "0.005"
                            Layout.preferredWidth: 80
                            selectByMouse: true
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: extractButton
                            objectName: "extractButton"
                            text: "提取湖泊"
                            enabled: !app.busyExtract && !app.busySeries
                            onClicked: app.extract(inputField.text,
                                                   Number(thresholdField.text),
                                                   Number(minAreaField.text))
                        }
                        BusyIndicator { running: app.busyExtract; visible: running }
                        Item { Layout.fillWidth: true }
                    }

                    // 分隔线
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#e8eaed" }

                    Label {
                        text: "时序检索（联网，分钟级）"
                        font.bold: true
                        font.pixelSize: 14
                        color: "#202124"
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 4
                        columnSpacing: 6
                        rowSpacing: 4

                        Label { text: "经度最小"; font.pixelSize: 12; color: "#5f6368" }
                        TextField {
                            id: x0Field
                            objectName: "x0Field"
                            text: "77.0249"
                            Layout.preferredWidth: 78
                            selectByMouse: true
                        }
                        Label { text: "纬度最小"; font.pixelSize: 12; color: "#5f6368" }
                        TextField {
                            id: y0Field
                            objectName: "y0Field"
                            text: "35.5427"
                            Layout.preferredWidth: 78
                            selectByMouse: true
                        }
                        Label { text: "经度最大"; font.pixelSize: 12; color: "#5f6368" }
                        TextField {
                            id: x1Field
                            objectName: "x1Field"
                            text: "77.3567"
                            Layout.preferredWidth: 78
                            selectByMouse: true
                        }
                        Label { text: "纬度最大"; font.pixelSize: 12; color: "#5f6368" }
                        TextField {
                            id: y1Field
                            objectName: "y1Field"
                            text: "35.8121"
                            Layout.preferredWidth: 78
                            selectByMouse: true
                        }
                        Label { text: "起始年"; font.pixelSize: 12; color: "#5f6368" }
                        TextField {
                            id: year0Field
                            objectName: "year0Field"
                            // 默认 3 年而不是 10 年：实测单景 9–30 s、一年 15–25 个
                            // 日期，10 年要跑半小时以上（docs/时序检索为什么慢.md）。
                            // 默认值应该是"点下去能等到结果"的那个量级。
                            text: "2023"
                            Layout.preferredWidth: 78
                            selectByMouse: true
                        }
                        Label { text: "结束年"; font.pixelSize: 12; color: "#5f6368" }
                        TextField {
                            id: year1Field
                            objectName: "year1Field"
                            text: "2025"
                            Layout.preferredWidth: 78
                            selectByMouse: true
                        }
                    }

                    // 代价提示：联网读一景 9–30 s，一年 15–25 个日期。
                    // 不给这个数，用户会以为"等 10 分钟没反应"是程序卡了。
                    Label {
                        objectName: "seriesCostLabel"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        font.pixelSize: 11
                        color: "#5f6368"
                        text: "联网读景，每年约 1–4 分钟（单景 9–30 秒）。"
                              + "每年数越多越慢，建议先跑 1 年看看。"
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: seriesButton
                            objectName: "seriesButton"
                            text: "检索时序"
                            enabled: !app.busyExtract && !app.busySeries
                            onClicked: app.buildSeries(
                                [Number(x0Field.text), Number(y0Field.text),
                                 Number(x1Field.text), Number(y1Field.text)],
                                parseInt(year0Field.text), parseInt(year1Field.text))
                        }
                        BusyIndicator { running: app.busySeries; visible: running }
                        // 取消入口：分钟级联网检索必须可以中止（worker.py 的
                        // SeriesWorker.cancel 本来就有，缺的是够得着的按钮）。
                        // 只在检索进行中出现；能否立即停取决于回调间隙，
                        // 诚实语义由 SeriesWorker.cancel() 的 docstring 交代。
                        Button {
                            id: cancelSeriesButton
                            objectName: "cancelSeriesButton"
                            text: "取消检索"
                            visible: app.busySeries
                            onClicked: app.cancelSeries()
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // 分隔线
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#e8eaed" }

                    // ---- 最新观测（联网，秒级到十几秒）----
                    // 名字一律叫「最新观测」，不叫"实时"：Sentinel-2 重访 5 天 +
                    // L2A 处理 1–3 天，叫实时是承诺做不到的事（docs/最新观测契约.md）。
                    // 这里最要紧的是数据新鲜度：观测日期 + 距今 N 天永远显示，
                    // 且"没量到"绝不显示成 0.00 km²（NaN 走单独一条文案）。
                    Label {
                        text: "最新观测（联网）"
                        font.bold: true
                        font.pixelSize: 14
                        color: "#202124"
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: latestButton
                            objectName: "latestButton"
                            text: "刷新最新观测"
                            enabled: !app.busyExtract && !app.busySeries && !app.latestBusy
                            onClicked: app.refreshLatest(
                                [Number(x0Field.text), Number(y0Field.text),
                                 Number(x1Field.text), Number(y1Field.text)],
                                app.latestLookbackDays)
                        }
                        BusyIndicator { running: app.latestBusy; visible: running }
                        Item { Layout.fillWidth: true }
                    }

                    // 加载中提示（照 seriesButton 的处理：busy 时显示一行）
                    Label {
                        objectName: "latestLoadingLabel"
                        Layout.fillWidth: true
                        visible: app.latestBusy
                        text: "正在检索最新影像 …"
                        wrapMode: Text.Wrap
                        font.pixelSize: 11
                        color: "#5f6368"
                    }

                    // 结果区。三种状态分开措辞：
                    //   a) 没观测（latestAgeDays < 0）  -> "暂无可用观测"
                    //   b) 有观测但没量到面积（NaN）    -> "面积没量到（无水面信号）" + 季节警告
                    //   c) 正常                          -> 日期 / 距今天数 / 面积 / 湖泊数
                    ColumnLayout {
                        objectName: "latestPanel"
                        Layout.fillWidth: true
                        visible: !app.latestBusy && app.latestAgeDays >= 0
                        spacing: 2

                        // 新鲜度：日期 + 距今 N 天。这是本功能诚实性的核心。
                        Label {
                            objectName: "latestFreshnessLabel"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.pixelSize: 12
                            font.bold: true
                            color: "#202124"
                            text: "观测日期 " + app.latestDate
                                  + "　距今 " + app.latestAgeDays + " 天"
                        }

                        Label {
                            objectName: "latestAreaLabel"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.pixelSize: 12
                            // NaN 判断：QML 里 Number.isNaN 最稳；面积没量到时
                            // 绝不显示 0.00 km²（缺数据返回 None 的项目硬规矩）。
                            color: Number.isNaN(app.latestAreaKm2) ? "#b3261e" : "#1e8e3e"
                            text: Number.isNaN(app.latestAreaKm2)
                                  ? "面积没量到（无水面信号）"
                                  : "当前面积 " + Number(app.latestAreaKm2).toFixed(4)
                                    + " km²　湖泊 " + app.latestNLakes + " 个"
                        }

                        Label {
                            objectName: "latestMessageLabel"
                            Layout.fillWidth: true
                            visible: app.latestMessage !== ""
                            wrapMode: Text.Wrap
                            font.pixelSize: 11
                            color: "#b3261e"
                            text: app.latestMessage
                        }
                    }

                    // a) 没观测：明确说"暂无可用观测"，不说"没有湖"
                    Label {
                        objectName: "latestEmptyLabel"
                        Layout.fillWidth: true
                        visible: !app.latestBusy && app.latestAgeDays < 0
                                 && app.latestMessage !== ""
                        wrapMode: Text.Wrap
                        font.pixelSize: 11
                        color: "#5f6368"
                        text: app.latestMessage
                    }

                    Item { Layout.fillHeight: true }  // 弹性占位，把提示顶到面板底部

                    Label {
                        Layout.fillWidth: true
                        text: "提示：默认影像（2025-09-30 小景）湖面冰封/高浊度，"
                              + "NDWI 最高约 0.167，阈值 0.15 下提取 0 条属正常结果，"
                              + "可降低阈值重试。"
                        wrapMode: Text.Wrap
                        font.pixelSize: 11
                        color: "#5f6368"
                    }
                    }
                }
            }

            // ---- 中：地图（铺满剩余宽度） ----
            MapView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 240
            }
        }

        // ---- 底：时序曲线 / 湖泊列表切换 ----
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 330
            color: "white"
            radius: 6

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 0

                TabBar {
                    id: bottomBar
                    objectName: "bottomBar"
                    Layout.fillWidth: true
                    // 页签带 objectName：冒烟需要真实点击切页后再驱动表格联动
                    TabButton { objectName: "tabSeries"; text: "面积时序" }
                    TabButton { objectName: "tabTable"; text: "湖泊列表" }
                }

                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: bottomBar.currentIndex

                    SeriesChart { objectName: "seriesChart" }
                    LakeTable { id: table; objectName: "lakeTable" }
                }
            }
        }
    }

    // ---- 状态栏：悬停经纬度 + 曲线点选面积 + 选中湖风险摘要 + 进度 ----
    // 用普通 Rectangle 而不是 Frame 定制 background：原生样式（Windows）不支持
    // 定制 Controls 的背景件，会打 "does not support customization" 警告。
    footer: Rectangle {
        implicitHeight: 30
        color: "#e8eaed"

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 10
            anchors.rightMargin: 10
            spacing: 16

            Label {
                objectName: "statusCoord"
                text: window.coordText
                font.pixelSize: 12
                color: "#202124"
            }
            Label {
                objectName: "statusPoint"
                text: window.pointText
                font.pixelSize: 12
                color: "#1a56b0"
            }
            // 选中湖风险摘要（等级 + 判据值，_apply_series_result 判定回流）。
            // 无选中/未判定为 "" 整个隐藏；配色保持中性——等级语义色由表格
            // 色块与地图多边形承担，状态栏不再涂一遍，避免 LOW 也被衬红。
            Label {
                objectName: "statusRisk"
                visible: app.selectedLakeRisk !== ""
                text: app.selectedLakeLabel !== ""
                      ? app.selectedLakeLabel + "　" + app.selectedLakeRisk
                      : app.selectedLakeRisk
                font.pixelSize: 12
                font.bold: true
                color: "#202124"
                elide: Text.ElideMiddle
                Layout.maximumWidth: 480
            }
            Item { Layout.fillWidth: true }
            Label {
                objectName: "statusProgress"
                text: app.progressText
                font.pixelSize: 12
                color: "#5f6368"
            }
        }
    }

    // ------------------------------------------------------------------
    // 联动（回环防法见文件头注释）
    // ------------------------------------------------------------------

    // 地图 → 表格：悬停报坐标；点湖联动表格（selectRow 静默，不成环）
    Connections {
        target: app.map
        function onMouseMoved(lon, lat) {
            window.coordText = (isNaN(lon) || isNaN(lat))
                ? "悬停：—"
                : "悬停：" + Number(lon).toFixed(5) + "°E, "
                            + Number(lat).toFixed(5) + "°N"
        }
        function onLakeClicked(lakeId) {
            // 点空白（-1）：清除地图高亮与表格选中色；点中湖：高亮多边形并把表格
            // 滚动到该行（selectRow 同时更新详情面板，且不发 lakeClicked）
            app.selectLake(lakeId)
            if (lakeId >= 0) {
                table.highlightedId = lakeId
                table.selectRow(lakeId)
            } else {
                table.highlightedId = -1
            }
        }
    }

    // 表格 → 地图：用户点行 → 高亮地图多边形。_activate 里已设 highlightedId，
    // 这里按装配契约再设一次（幂等），真正驱动地图的是 app.selectLake。
    Connections {
        target: table
        function onLakeClicked(lakeId) {
            app.selectLake(lakeId)
            table.highlightedId = lakeId
        }
    }

    // 曲线点选 → 状态栏显示该期面积（SeriesController.chartClicked → pointClicked）
    Connections {
        target: app.series
        function onPointClicked(date) {
            var pts = app.series.points
            for (var i = 0; i < pts.length; ++i) {
                if (pts[i].date === date) {
                    window.pointText = "点选 " + date + "：面积 "
                                       + Number(pts[i].areaKm2).toFixed(4) + " km²"
                    return
                }
            }
            window.pointText = "点选 " + date + "：不在当前序列"
        }
    }
}
