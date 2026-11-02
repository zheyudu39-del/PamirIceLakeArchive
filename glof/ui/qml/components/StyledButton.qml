import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// 统一样式的按钮。三种视觉层级：
//   primary   实心强调色——每个区块只有一个，代表"这里的主操作"
//   secondary 描边——次要操作
//   ghost     无边框——取消、辅助
//
// 为什么不用 Material 风格：Qt Quick Controls 的 Material 需要额外样式插件，
// 且会把所有控件一起改掉（包括我们不希望变的 TableView）。这里只包一层
// Button，背景自绘，其余控件保持原生行为——冒烟脚本对原生控件的行为假设
// 全部继续成立。
Button {
    id: control

    property string variant: "secondary"
    property bool compact: false

    readonly property bool isPrimary: variant === "primary"
    readonly property bool isGhost: variant === "ghost"

    implicitHeight: compact ? 26 : Theme.controlHeight
    // 宽度按文字实测算，不用 contentItem.implicitWidth——那个值在
    // Layout.fillWidth 下会反过来受父级约束，形成循环依赖，实测表现为
    // 文字被挤成两行（"提取/湖泊"）。
    //
    // 用不可见的 Text 量宽度而不是 TextMetrics：TextMetrics 在部分
    // Qt 6.x 里不是默认可见类型，实测报 "TextMetrics was not found"。
    // 隐藏的 Text 任何版本都有，且不参与布局。
    implicitWidth: Math.max(compact ? 72 : 96, measure.implicitWidth + (compact ? 20 : 28))
    padding: 0

    Text {
        id: measure
        visible: false
        font: control.font
        text: control.text
    }

    font.pixelSize: Theme.fontBody
    font.bold: isPrimary

    // 内容自绘，把原生按钮的默认文字隐藏
    contentItem: Text {
        id: label
        text: control.text
        font: control.font
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        color: !control.enabled
               ? Theme.textMuted
               : (control.isPrimary ? Theme.textOnAccent
                  : (control.isGhost ? Theme.textSecondary : Theme.textPrimary))
    }

    background: Rectangle {
        radius: Theme.radiusSm
        color: {
            if (!control.enabled)
                return control.isPrimary ? "#c3cbd4" : Theme.fieldBg;
            if (control.isPrimary) {
                if (control.down) return Theme.accentPressed;
                if (control.hovered) return Theme.accentHover;
                return Theme.accent;
            }
            if (control.isGhost) {
                if (control.down) return Theme.divider;
                if (control.hovered) return Theme.accentSoft;
                return "transparent";
            }
            if (control.down) return Theme.divider;
            if (control.hovered) return Theme.accentSoft;
            return Theme.fieldBg;
        }
        border.width: (control.isPrimary || control.isGhost) ? 0 : 1
        border.color: control.hovered ? Theme.fieldBorderFocus : Theme.fieldBorder

        Behavior on color { ColorAnimation { duration: 90 } }
    }
}
