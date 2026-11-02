// 主窗口装配：左侧参数面板 + 中间地图 + 底部"面积时序 / 湖泊列表"切换 + 状态栏。
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
// 视觉结构（2026-11-03 改版）：
//   顶栏（应用名 + 副标题 + 全局状态） / 左面板（三段卡片） / 中地图 / 底部分页。
//   颜色与间距全部来自 Theme.qml 单例，不在这里写死色值——改版前 4 个 QML 里
//   散着 20+ 个硬编码色值，改一处配色要全文搜索还必然漏。
//   **所有 objectName 与绑定保持原样**，冒烟脚本依赖它们。
//
// 参数默认值与 data/kyagar 契约素材一致。诚实提示：该景（2025-09-30 小景）
// NDWI 最高约 0.167，阈值 0.15 下提出 0 条是正常结果，面板上写明而不是让
// 用户以为程序坏了。

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

ApplicationWindow {
    id: window

    objectName: "mainWindow"
    width: 1440
    height: 900
    minimumWidth: 1100
    minimumHeight: 700
    visible: true
    title: "帕米尔冰湖档案"
    color: Theme.windowBg

    // ---- 状态栏文本（Connections 里回填，见文件底部） ----
    property string coordText: "悬停：—"
    property string pointText: "点选：—"

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ================================================================
        // 顶栏：应用标识 + 全局进度/错误
        // ================================================================
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            color: Theme.panelBg

            // 顶栏下沿细线，把标题区和内容区分开
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: Theme.panelBorder
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Theme.gapLg
                anchors.rightMargin: Theme.gapLg
                spacing: Theme.gapMd

                // 应用图标：一个抽象的湖面 + 山峰，纯几何绘制，不引外部资源
                Item {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: 28
                    Layout.preferredHeight: 28

                    Rectangle {
                        anchors.fill: parent
                        radius: 7
                        gradient: Gradient {
                            GradientStop { position: 0.0; color: Theme.accent }
                            GradientStop { position: 1.0; color: "#1e40af" }
                        }
                    }
                    // 山峰：两个三角形
                    Canvas {
                        anchors.centerIn: parent
                        width: 20
                        height: 16
                        onPaint: {
                            var ctx = getContext("2d");
                            ctx.reset();
                            ctx.fillStyle = "#ffffff";
                            // 左峰
                            ctx.beginPath();
                            ctx.moveTo(2, 14);
                            ctx.lineTo(8, 3);
                            ctx.lineTo(13, 14);
                            ctx.closePath();
                            ctx.fill();
                            // 右峰（略矮，压在山脊后）
                            ctx.globalAlpha = 0.72;
                            ctx.beginPath();
                            ctx.moveTo(10, 14);
                            ctx.lineTo(15, 7);
                            ctx.lineTo(19, 14);
                            ctx.closePath();
                            ctx.fill();
                        }
                    }
                }

                ColumnLayout {
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 0

                    Text {
                        text: "帕米尔冰湖档案"
                        font.pixelSize: Theme.fontTitle
                        font.bold: true
                        color: Theme.textPrimary
                    }
                    Text {
                        text: "冰湖识别 · 溃决风险筛查 · 可审计"
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textMuted
                    }
                }

                Item { Layout.fillWidth: true }

                // 全局状态胶囊：有错误变红，有进度变蓝，都没有则隐藏。
                // 替代改版前横跨整个窗口的横幅——横幅会挤压布局，且在
                // 没有消息时占一行空白。
                Rectangle {
                    objectName: "banner"
                    Layout.alignment: Qt.AlignVCenter
                    visible: app.lastError !== "" || app.progressText !== ""
                    implicitWidth: Math.min(bannerLabel.implicitWidth + 28, 520)
                    implicitHeight: 30
                    radius: 15
                    color: app.lastError !== "" ? Theme.dangerSoft : Theme.accentSoft
                    border.width: 1
                    border.color: app.lastError !== "" ? "#f5c2bd" : "#c6dafc"

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12
                        spacing: 6

                        // 状态点
                        Rectangle {
                            Layout.alignment: Qt.AlignVCenter
                            width: 7
                            height: 7
                            radius: 3.5
                            color: app.lastError !== "" ? Theme.danger : Theme.accent
                        }

                        Text {
                            id: bannerLabel
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            elide: Text.ElideRight
                            text: app.lastError !== "" ? app.lastError : app.progressText
                            color: app.lastError !== "" ? Theme.danger : Theme.accentPressed
                            font.pixelSize: Theme.fontBody
                        }
                    }
                }
            }
        }

        // ================================================================
        // 主区：左面板 + 地图
        // ================================================================
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: Theme.gapMd
            spacing: Theme.gapMd

            // ---- 左：参数面板 ----
            Card {
                Layout.preferredWidth: Theme.panelWidth
                Layout.fillHeight: true
                padding: 0

                // 面板内容比窗口高时（最小窗口 1100×700 实测）必须能滚：
                // 直接 ColumnLayout 会把"最新观测"整段裁在可视区外，
                // 用户以为按钮不存在。Flickable 不影响任何 objectName/绑定。
                Flickable {
                    id: panelFlick
                    objectName: "panelFlick"
                    anchors.fill: parent
                    anchors.margins: Theme.gapMd
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
                        spacing: Theme.gapSm

                    // ---- 区块 1：影像提取 ----
                    SectionHeader {
                        Layout.topMargin: 2
                        title: "影像提取"
                        subtitle: "本地 Sentinel-2 影像 → 湖泊清单"
                    }

                    Text {
                        text: "影像路径"
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textSecondary
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.gapSm

                        StyledField {
                            id: inputField
                            objectName: "inputField"
                            Layout.fillWidth: true
                            text: app.defaultInput
                            font.pixelSize: Theme.fontCaption
                            implicitHeight: 28
                            placeholderText: "直接粘贴路径，或点右侧「浏览…」"
                        }

                        // 「浏览…」用 secondary 而不是 primary：这个区块唯一的
                        // 主操作是「提取湖泊」，两个实心按钮会抢注意力。
                        StyledButton {
                            id: browseButton
                            objectName: "browseButton"
                            variant: "secondary"
                            compact: true
                            text: "浏览…"
                            implicitHeight: 28
                            Layout.alignment: Qt.AlignVCenter
                            enabled: !app.busyExtract && !app.busySeries
                            onClicked: fileDialog.open()
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.gapSm

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: "NDWI 阈值"
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textSecondary
                            }
                            StyledField {
                                id: thresholdField
                                objectName: "thresholdField"
                                Layout.fillWidth: true
                                text: "0.15"
                                horizontalAlignment: TextInput.AlignRight
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: "最小面积 km²"
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textSecondary
                            }
                            StyledField {
                                id: minAreaField
                                objectName: "minAreaField"
                                Layout.fillWidth: true
                                text: "0.005"
                                horizontalAlignment: TextInput.AlignRight
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.gapSm

                        StyledButton {
                            id: extractButton
                            objectName: "extractButton"
                            variant: "primary"
                            text: "提取湖泊"
                            Layout.fillWidth: true
                            enabled: !app.busyExtract && !app.busySeries
                            onClicked: app.extract(inputField.text,
                                                   Number(thresholdField.text),
                                                   Number(minAreaField.text))
                        }
                        BusyIndicator {
                            running: app.busyExtract
                            visible: running
                            implicitWidth: 26
                            implicitHeight: 26
                        }
                    }

                    // 分隔线
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.gapSm
                        height: 1
                        color: Theme.divider
                    }

                    // ---- 区块 2：时序检索 ----
                    SectionHeader {
                        Layout.topMargin: Theme.gapSm
                        title: "时序检索"
                        subtitle: "逐年联网取景，看湖面积怎么变"
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: Theme.gapSm
                        rowSpacing: 6

                        // 每个字段：上方小标签 + 下方输入框，纵向堆叠。
                        // 改版前是 4 列横排（标签-框-标签-框），列宽被标签
                        // 吃掉一半，输入框窄到看不清 77.0249 这种数。
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: "经度范围"
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textSecondary
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                StyledField {
                                    id: x0Field
                                    objectName: "x0Field"
                                    Layout.fillWidth: true
                                    text: "77.0249"
                                    font.pixelSize: Theme.fontCaption
                                    implicitHeight: 28
                                    horizontalAlignment: TextInput.AlignRight
                                }
                                Text {
                                    text: "–"
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.fontCaption
                                }
                                StyledField {
                                    id: x1Field
                                    objectName: "x1Field"
                                    Layout.fillWidth: true
                                    text: "77.3567"
                                    font.pixelSize: Theme.fontCaption
                                    implicitHeight: 28
                                    horizontalAlignment: TextInput.AlignRight
                                }
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: "纬度范围"
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textSecondary
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                StyledField {
                                    id: y0Field
                                    objectName: "y0Field"
                                    Layout.fillWidth: true
                                    text: "35.5427"
                                    font.pixelSize: Theme.fontCaption
                                    implicitHeight: 28
                                    horizontalAlignment: TextInput.AlignRight
                                }
                                Text {
                                    text: "–"
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.fontCaption
                                }
                                StyledField {
                                    id: y1Field
                                    objectName: "y1Field"
                                    Layout.fillWidth: true
                                    text: "35.8121"
                                    font.pixelSize: Theme.fontCaption
                                    implicitHeight: 28
                                    horizontalAlignment: TextInput.AlignRight
                                }
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: "年份范围"
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textSecondary
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                StyledField {
                                    id: year0Field
                                    objectName: "year0Field"
                                    // 默认 3 年而不是 10 年：实测单景 9–30 s、一年 15–25 个
                                    // 日期，10 年要跑半小时以上（docs/时序检索为什么慢.md）。
                                    // 默认值应该是"点下去能等到结果"的那个量级。
                                    text: "2023"
                                    Layout.fillWidth: true
                                    font.pixelSize: Theme.fontCaption
                                    implicitHeight: 28
                                    horizontalAlignment: TextInput.AlignRight
                                }
                                Text {
                                    text: "–"
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.fontCaption
                                }
                                StyledField {
                                    id: year1Field
                                    objectName: "year1Field"
                                    text: "2025"
                                    Layout.fillWidth: true
                                    font.pixelSize: Theme.fontCaption
                                    implicitHeight: 28
                                    horizontalAlignment: TextInput.AlignRight
                                }
                            }
                        }
                    }

                    // 代价提示：联网读一景 9–30 s，一年 15–25 个日期。
                    // 不给这个数，用户会以为"等 10 分钟没反应"是程序卡了。
                    Text {
                        objectName: "seriesCostLabel"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textMuted
                        text: "联网读景，每年约 1–4 分钟（单景 9–30 秒）。"
                              + "每年数越多越慢，建议先跑 1 年看看。"
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.gapSm

                        StyledButton {
                            id: seriesButton
                            objectName: "seriesButton"
                            variant: "primary"
                            text: "检索时序"
                            Layout.fillWidth: true
                            enabled: !app.busyExtract && !app.busySeries
                            onClicked: app.buildSeries(
                                [Number(x0Field.text), Number(y0Field.text),
                                 Number(x1Field.text), Number(y1Field.text)],
                                parseInt(year0Field.text), parseInt(year1Field.text))
                        }
                        BusyIndicator {
                            running: app.busySeries
                            visible: running
                            implicitWidth: 26
                            implicitHeight: 26
                        }
                        // 取消入口：分钟级联网检索必须可以中止（worker.py 的
                        // SeriesWorker.cancel 本来就有，缺的是够得着的按钮）。
                        // 只在检索进行中出现；能否立即停取决于回调间隙，
                        // 诚实语义由 SeriesWorker.cancel() 的 docstring 交代。
                        StyledButton {
                            id: cancelSeriesButton
                            objectName: "cancelSeriesButton"
                            variant: "secondary"
                            text: "取消"
                            visible: app.busySeries
                            onClicked: app.cancelSeries()
                        }
                    }

                    // 分隔线
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.gapSm
                        height: 1
                        color: Theme.divider
                    }

                    // ---- 区块 3：最新观测（联网，秒级到十几秒）----
                    // 名字一律叫「最新观测」，不叫"实时"：Sentinel-2 重访 5 天 +
                    // L2A 处理 1–3 天，叫实时是承诺做不到的事（docs/最新观测契约.md）。
                    // 这里最要紧的是数据新鲜度：观测日期 + 距今 N 天永远显示，
                    // 且"没量到"绝不显示成 0.00 km²（NaN 走单独一条文案）。
                    SectionHeader {
                        Layout.topMargin: Theme.gapSm
                        title: "最新观测"
                        subtitle: "拉取最近一景，影像直接上地图"
                    }

                    StyledButton {
                        id: latestButton
                        objectName: "latestButton"
                        variant: "secondary"
                        text: "刷新最新观测"
                        Layout.fillWidth: true
                        enabled: !app.busyExtract && !app.busySeries && !app.latestBusy
                        onClicked: app.refreshLatest(
                            [Number(x0Field.text), Number(y0Field.text),
                             Number(x1Field.text), Number(y1Field.text)],
                            app.latestLookbackDays)
                    }

                    // 常驻状态卡：未请求 / 加载 / 有观测 / 无可用观测。
                    // 改版前没观测时整块消失，用户看不到这里会返回什么；
                    // 加载时旧结果突然消失、结果回来又把下面布局顶下去。
                    // 现在卡片占同样的位置，只切内部状态。
                    Rectangle {
                        Layout.fillWidth: true
                        visible: app.latestBusy
                        implicitHeight: latestLoadingCol.implicitHeight + 16
                        radius: Theme.radiusSm
                        color: Theme.accentSoft
                        border.width: 1
                        border.color: "#c6dafc"

                        ColumnLayout {
                            id: latestLoadingCol
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 6

                            RowLayout {
                                spacing: Theme.gapSm
                                BusyIndicator {
                                    running: app.latestBusy
                                    implicitWidth: 20
                                    implicitHeight: 20
                                }
                                Text {
                                    objectName: "latestLoadingLabel"
                                    Layout.fillWidth: true
                                    text: "正在联网取最近一景并读取影像…"
                                    wrapMode: Text.Wrap
                                    font.pixelSize: Theme.fontCaption
                                    font.bold: true
                                    color: Theme.accentPressed
                                }
                            }
                            Text {
                                text: "通常需要十几秒；保留上一景直到新影像就绪。"
                                wrapMode: Text.Wrap
                                font.pixelSize: Theme.fontCaption
                                color: Theme.textSecondary
                            }
                        }
                    }

                    // 结果区：
                    //   a) 有观测但没量到面积（NaN） -> "面积没量到" + 原因
                    //   b) 正常                       -> 日期 / 距今天数 / 面积 / 湖泊数
                    Rectangle {
                        objectName: "latestPanel"
                        Layout.fillWidth: true
                        visible: !app.latestBusy && app.latestAgeDays >= 0
                        implicitHeight: latestCol.implicitHeight + 16
                        radius: Theme.radiusSm
                        color: Number.isNaN(app.latestAreaKm2) ? Theme.warningSoft
                                                               : Theme.successSoft
                        border.width: 1
                        border.color: Number.isNaN(app.latestAreaKm2) ? "#f0d9a8"
                                                                       : "#b7dfbf"

                        ColumnLayout {
                            id: latestCol
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 5

                            Text {
                                Layout.fillWidth: true
                                text: Number.isNaN(app.latestAreaKm2)
                                      ? "● 观测有效，但未量到湖面"
                                      : "● 已获取最新可用影像"
                                font.pixelSize: Theme.fontCaption
                                font.bold: true
                                color: Number.isNaN(app.latestAreaKm2)
                                       ? Theme.warning : Theme.success
                            }

                            Text {
                                objectName: "latestFreshnessLabel"
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                font.pixelSize: Theme.fontBody
                                color: Theme.textPrimary
                                text: "观测日期 " + app.latestDate
                                      + "　距今 " + app.latestAgeDays + " 天"
                            }

                            Text {
                                objectName: "latestAreaLabel"
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                font.pixelSize: Theme.fontBody
                                font.bold: true
                                // 面积没量到时绝不显示 0.00 km²。
                                color: Number.isNaN(app.latestAreaKm2)
                                       ? Theme.danger : Theme.success
                                text: Number.isNaN(app.latestAreaKm2)
                                      ? "面积没量到（无水面信号）"
                                      : "当前面积 " + Number(app.latestAreaKm2).toFixed(4)
                                        + " km²　湖泊 " + app.latestNLakes + " 个"
                            }

                            Text {
                                objectName: "latestMessageLabel"
                                Layout.fillWidth: true
                                visible: app.latestMessage !== ""
                                wrapMode: Text.Wrap
                                font.pixelSize: Theme.fontCaption
                                color: Theme.warning
                                text: app.latestMessage
                            }
                        }
                    }

                    // 未请求 / 无可用观测 / 检索失败：卡片永远可见。
                    Text {
                        objectName: "latestEmptyLabel"
                        Layout.fillWidth: true
                        visible: !app.latestBusy && app.latestAgeDays < 0
                        wrapMode: Text.Wrap
                        font.pixelSize: Theme.fontCaption
                        color: app.latestMessage === "" ? Theme.textMuted
                               : (app.lastError !== "" ? Theme.danger : Theme.textSecondary)
                        text: app.latestMessage !== ""
                              ? app.latestMessage
                              : "尚未刷新。点击上方按钮后，会显示观测日期、距今天数，"
                                + "并在地图中显示该景影像。"
                    }

                    Item { Layout.fillHeight: true }  // 弹性占位，把提示顶到面板底部

                    // 底部说明条：改版前是一段裸文字，和输入框混在一起。
                    // 装进浅色小框后，视觉上明确"这是注记，不是可操作项"。
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.topMargin: Theme.gapSm
                        implicitHeight: hintText.implicitHeight + 14
                        radius: Theme.radiusSm
                        color: Theme.warningSoft

                        Text {
                            id: hintText
                            anchors.fill: parent
                            anchors.margins: 7
                            text: "默认影像（2025-09-30 小景）湖面冰封/高浊度，"
                                  + "NDWI 最高约 0.167，阈值 0.15 下提取 0 条属正常结果，"
                                  + "可降低阈值重试。"
                            wrapMode: Text.Wrap
                            font.pixelSize: Theme.fontCaption
                            color: Theme.warning
                        }
                    }
                    }
                }
            }

            // ---- 中：地图（铺满剩余宽度） ----
            Card {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 240
                padding: 0

                // Card 的白底会被地图自己的深色底盖住，所以这里把卡片
                // 当成"圆角裁剪容器"用：地图铺满，圆角由 Card 提供。
                MapView {
                    anchors.fill: parent
                    anchors.margins: 1
                    radius: Theme.radiusMd - 1
                }
            }
        }

        // ================================================================
        // 底部：时序曲线 / 湖泊列表
        // ================================================================
        Card {
            Layout.fillWidth: true
            Layout.preferredHeight: 340
            Layout.leftMargin: Theme.gapMd
            Layout.rightMargin: Theme.gapMd
            Layout.bottomMargin: Theme.gapMd
            padding: 0

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 1
                spacing: 0

                // 页签自绘：改版前是原生 TabBar，指示条细且灰，选中态不明显。
                TabBar {
                    id: bottomBar
                    objectName: "bottomBar"
                    Layout.fillWidth: true

                    background: Rectangle {
                        color: Theme.panelBg
                        // 底部细线，和内容区分开
                        Rectangle {
                            anchors.bottom: parent.bottom
                            width: parent.width
                            height: 1
                            color: Theme.divider
                        }
                    }

                    // 页签带 objectName：冒烟需要真实点击切页后再驱动表格联动
                    TabButton {
                        objectName: "tabSeries"
                        text: "面积时序"
                        implicitHeight: 38
                        implicitWidth: 108

                        contentItem: Text {
                            text: parent.text
                            font.pixelSize: Theme.fontBody
                            font.bold: parent.checked
                            color: parent.checked ? Theme.accent : Theme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Item {
                            // 选中指示条：底部 2px 强调色
                            Rectangle {
                                anchors.bottom: parent.bottom
                                anchors.horizontalCenter: parent.horizontalCenter
                                width: parent.width * (parent.parent.checked ? 0.72 : 0)
                                height: 2
                                radius: 1
                                color: Theme.accent
                                Behavior on width { NumberAnimation { duration: 140 } }
                            }
                        }
                    }
                    TabButton {
                        objectName: "tabTable"
                        text: "湖泊列表"
                        implicitHeight: 38
                        implicitWidth: 108

                        contentItem: Text {
                            text: parent.text
                            font.pixelSize: Theme.fontBody
                            font.bold: parent.checked
                            color: parent.checked ? Theme.accent : Theme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Item {
                            Rectangle {
                                anchors.bottom: parent.bottom
                                anchors.horizontalCenter: parent.horizontalCenter
                                width: parent.width * (parent.parent.checked ? 0.72 : 0)
                                height: 2
                                radius: 1
                                color: Theme.accent
                                Behavior on width { NumberAnimation { duration: 140 } }
                            }
                        }
                    }
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
        implicitHeight: 32
        color: Theme.footerBg

        Rectangle {
            anchors.top: parent.top
            width: parent.width
            height: 1
            color: Theme.panelBorder
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.gapLg
            anchors.rightMargin: Theme.gapLg
            spacing: Theme.gapLg

            Text {
                objectName: "statusCoord"
                text: window.coordText
                font.pixelSize: Theme.fontCaption
                color: Theme.textSecondary
            }
            Text {
                objectName: "statusPoint"
                text: window.pointText
                font.pixelSize: Theme.fontCaption
                color: Theme.accent
            }
            // 选中湖风险摘要（等级 + 判据值，_apply_series_result 判定回流）。
            // 无选中/未判定为 "" 整个隐藏；配色保持中性——等级语义色由表格
            // 色块与地图多边形承担，状态栏不再涂一遍，避免 LOW 也被衬红。
            Text {
                objectName: "statusRisk"
                visible: app.selectedLakeRisk !== ""
                text: app.selectedLakeLabel !== ""
                      ? app.selectedLakeLabel + "　" + app.selectedLakeRisk
                      : app.selectedLakeRisk
                font.pixelSize: Theme.fontCaption
                font.bold: true
                color: Theme.textPrimary
                elide: Text.ElideMiddle
                Layout.maximumWidth: 480
            }
            Item { Layout.fillWidth: true }
            Text {
                objectName: "statusProgress"
                text: app.progressText
                font.pixelSize: Theme.fontCaption
                color: Theme.textMuted
                elide: Text.ElideRight
                Layout.maximumWidth: 420
            }
        }
    }

    // ------------------------------------------------------------------
    // 选影像：打开系统文件对话框
    // ------------------------------------------------------------------
    // 用 QtQuick.Dialogs 的 FileDialog 而不是 Python 侧 QFileDialog：
    // 前者是 QML 原生对话框，走同一套主题；后者要再从 Python 回调改 QML
    // 文本，多一层跨语言往返，且 offscreen 冒烟下更容易挂。
    //
    // 只列 .tif/.tiff，但保留「所有文件」——用户手上的多波段 GeoTIFF 不一定
    // 是这个后缀。选完不在这里校验能不能读：读不了由提取流程如实报错，
    // 比对话框里猜一个"文件无效"更准确。
    FileDialog {
        id: fileDialog
        objectName: "fileDialog"
        title: "选择 Sentinel-2 影像"
        // 起始目录落到契约素材旁边（见 AppState.defaultInputDir）。
        // Qt 6 的属性名是 currentFolder；Qt 5 的 folder 在这里不存在，
        // 写了会直接 "Cannot assign to non-existent property" 加载失败。
        currentFolder: app.defaultInputDir()
        nameFilters: [
            "Sentinel-2 / GeoTIFF 影像 (*.tif *.tiff *.vrt)",
            "所有文件 (*)"
        ]
        fileMode: FileDialog.OpenFile
        onAccepted: {
            // 转本地路径的活交给 Python：QML 自己删 "file://" 会留下
            // 中文/空格的百分号转义，rasterio 拿到的是个不存在的文件名。
            inputField.text = app.pathFromUrl(selectedFile)
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
