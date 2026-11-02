// 3D 回放页：地形视图 + 时间轴 + 出处说明。
//
// 布局自上而下：
//   1. 出处条——水位曲线从哪来、垂直方向是否压缩。必须常驻，不能只画不说。
//   2. 3D 视图（Lake3DView）。
//   3. 时间轴：拖动看每一期的湖面，旁边显示该期日期/面积/相对水位。
//
// 当前帧水位算不出来时，面积照常显示、水位显示"未知"——不显示 0，
// 0 在这套坐标里是"湖空了"，与"没算出来"不是一回事。

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Item {
    id: page

    readonly property var frames: app.lake3d ? app.lake3d.frames : []
    readonly property int frameCount: frames.length
    readonly property int currentIndex: app.lake3d ? app.lake3d.currentIndex : -1
    readonly property var currentFrame:
        (currentIndex >= 0 && currentIndex < frameCount) ? frames[currentIndex] : null

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.gapMd
        spacing: Theme.gapSm

        // ---- 1. 出处条 ----
        Rectangle {
            Layout.fillWidth: true
            visible: app.lake3d && app.lake3d.ready
            implicitHeight: sourceCol.implicitHeight + 14
            radius: Theme.radiusSm
            color: Theme.accentSoft
            border.width: 1
            border.color: "#c6dafc"

            ColumnLayout {
                id: sourceCol
                anchors.fill: parent
                anchors.margins: 7
                spacing: 3

                Text {
                    objectName: "lake3dSourceLabel"
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: app.lake3d ? app.lake3d.levelSource : ""
                    font.pixelSize: Theme.fontCaption
                    color: Theme.accentPressed
                }
                Text {
                    objectName: "lake3dScaleLabel"
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "地形垂直方向已压缩（真实高差 "
                          + (app.lake3d ? Math.round(app.lake3d.zMax - app.lake3d.zMin) : 0)
                          + " m）；湖面高度放大了 8 倍以便看清变化，"
                          + "时间轴上的百分比是未放大的真实值。"
                          + "湖面圆盘是示意形状，高度由实测面积反演。"
                    font.pixelSize: Theme.fontCaption
                    color: Theme.textSecondary
                }
                Text {
                    objectName: "lake3dMessageLabel"
                    Layout.fillWidth: true
                    visible: app.lake3d && app.lake3d.message !== ""
                    wrapMode: Text.Wrap
                    text: app.lake3d ? app.lake3d.message : ""
                    font.pixelSize: Theme.fontCaption
                    color: Theme.warning
                }
            }
        }

        // ---- 2. 3D 视图 ----
        Lake3DView {
            objectName: "lake3dView"
            Layout.fillWidth: true
            Layout.fillHeight: true
        }

        // ---- 3. 时间轴 ----
        Rectangle {
            Layout.fillWidth: true
            visible: page.frameCount > 0
            implicitHeight: timelineCol.implicitHeight + 16
            radius: Theme.radiusSm
            color: Theme.footerBg
            border.width: 1
            border.color: Theme.panelBorder

            ColumnLayout {
                id: timelineCol
                anchors.fill: parent
                anchors.margins: 8
                spacing: 6

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.gapMd

                    Text {
                        objectName: "lake3dDateLabel"
                        text: page.currentFrame ? page.currentFrame.date : "—"
                        font.pixelSize: Theme.fontLabel
                        font.bold: true
                        color: Theme.textPrimary
                    }
                    Text {
                        objectName: "lake3dAreaLabel"
                        text: page.currentFrame
                              ? "面积 " + Number(page.currentFrame.areaKm2).toFixed(4) + " km²"
                              : ""
                        font.pixelSize: Theme.fontLabel
                        color: Theme.textPrimary
                    }
                    Text {
                        objectName: "lake3dLevelLabel"
                        // 水位未知时显示"未知"，绝不显示 0
                        text: page.currentFrame
                              ? (page.currentFrame.levelKnown
                                 ? "相对水位 " + (page.currentFrame.relativeLevel * 100).toFixed(1) + "%"
                                   + "（约 " + Math.round(page.currentFrame.absoluteLevel) + " m）"
                                 : "水位未知（面积超出 DEM 曲线范围）")
                              : ""
                        font.pixelSize: Theme.fontBody
                        font.bold: true
                        color: (page.currentFrame && page.currentFrame.levelKnown)
                               ? Theme.success : Theme.danger
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: (page.currentIndex + 1) + " / " + page.frameCount
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textMuted
                    }
                }

                Slider {
                    id: slider
                    objectName: "lake3dSlider"
                    Layout.fillWidth: true
                    from: 0
                    to: Math.max(0, page.frameCount - 1)
                    stepSize: 1
                    snapMode: Slider.SnapAlways
                    value: page.currentIndex >= 0 ? page.currentIndex : 0
                    enabled: page.frameCount > 1
                    onMoved: app.lake3d.setCurrentIndex(Math.round(value))
                }

                // 逐期刻度：面积越高柱子越高，一眼看出蓄水—溃决的形状
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    visible: page.frameCount > 1

                    Repeater {
                        model: page.frameCount
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: 18
                            color: "transparent"

                            readonly property real rel:
                                page.frames[index] && page.frames[index].levelKnown
                                ? page.frames[index].relativeLevel : 0

                            Rectangle {
                                anchors.bottom: parent.bottom
                                width: parent.width
                                height: Math.max(1, parent.height * parent.rel)
                                color: index === page.currentIndex
                                       ? Theme.accent
                                       : (page.frames[index] && page.frames[index].levelKnown
                                          ? "#a8c4ea" : Theme.divider)
                            }
                        }
                    }
                }
            }
        }
    }
}
