import QtQuick
import QtQuick.Controls
import ".."

// 统一样式的输入框：聚焦时边框变强调色 + 一圈极淡的光晕。
// 原生 TextField 在 Windows 上是方角灰边，和圆角面板不搭。
TextField {
    id: control

    implicitHeight: Theme.controlHeight
    leftPadding: 10
    rightPadding: 10
    topPadding: 0
    bottomPadding: 0
    verticalAlignment: TextInput.AlignVCenter

    font.pixelSize: Theme.fontBody
    color: Theme.textPrimary
    placeholderTextColor: Theme.textMuted
    selectByMouse: true

    selectionColor: Theme.accent
    selectedTextColor: Theme.textOnAccent

    background: Rectangle {
        radius: Theme.radiusSm
        color: control.enabled ? Theme.fieldBg : "#f4f6f8"
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.fieldBorderFocus
                     : (control.hovered ? "#aeb7c2" : Theme.fieldBorder)
        // 聚焦光晕：外扩一圈淡蓝，比单纯加粗边框更容易注意到
        Rectangle {
            anchors.fill: parent
            anchors.margins: -3
            radius: parent.radius + 3
            color: "transparent"
            border.width: 3
            border.color: Theme.accent
            opacity: control.activeFocus ? 0.12 : 0
            Behavior on opacity { NumberAnimation { duration: 110 } }
        }
    }
}
