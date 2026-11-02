import QtQuick
import ".."

// 白底圆角卡片 + 极淡的投影。
//
// 改版前三块内容都是同一种白底圆角，没有前后关系。卡片加一层 1px 边框 +
// 很轻的投影后，"面板浮在窗口背景上"的关系就出来了，不需要更重的视觉手段。
//
// 投影用两层半透明圆角矩形叠出来（Qt Quick 的 DropShadow 需要
// QtGraphicalEffects，在 6.x 已拆成独立模块，能不加依赖就不加）。
Rectangle {
    id: card

    property int padding: Theme.gapMd

    radius: Theme.radiusMd
    color: Theme.panelBg
    border.width: 1
    border.color: Theme.panelBorder
    // 子项（地图影像、表格）会画到圆角之外。clip 让圆角真正起作用；
    // 不开的话左上角是直角，和其余三个圆角不一致（实测）。
    clip: true

    // 投影：往下错 1px 的淡灰，只画在卡片外侧下方
    Rectangle {
        z: -1
        anchors.fill: parent
        anchors.topMargin: 2
        anchors.leftMargin: 1
        anchors.rightMargin: 1
        radius: parent.radius
        color: "#000000"
        opacity: 0.05
    }
}
