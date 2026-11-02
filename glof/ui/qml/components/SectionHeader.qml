import QtQuick
import QtQuick.Layouts
import ".."

// 侧边栏区块标题：左边一根强调色竖条 + 标题 + 可选的副标题说明。
//
// 改版前的区块标题只是加粗文字，和下面的输入框混在一起，看不出"这是新一段"。
// 竖条是最省地方的分组信号：不占横向空间，扫视时眼睛能直接分段。
RowLayout {
    id: root

    property string title: ""
    property string subtitle: ""

    spacing: Theme.gapSm

    Rectangle {
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: 3
        Layout.preferredHeight: titleLabel.implicitHeight + (subtitle !== "" ? 14 : 0)
        radius: 1.5
        color: Theme.accent
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 1

        Text {
            id: titleLabel
            text: root.title
            font.pixelSize: Theme.fontSection
            font.bold: true
            color: Theme.textPrimary
        }

        Text {
            Layout.fillWidth: true
            visible: root.subtitle !== ""
            text: root.subtitle
            font.pixelSize: Theme.fontCaption
            color: Theme.textMuted
            wrapMode: Text.Wrap
        }
    }
}
