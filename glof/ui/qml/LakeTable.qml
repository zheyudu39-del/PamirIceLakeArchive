// 湖泊/风险列表：TableView + 判据详情面板（glof.ui 的 lake_table 组件）。
//
// 信号回环的防法（本文件最重要的一条注释）：
//     地图点湖 → 主窗口调 selectRow(lakeId) → 若 selectRow 再发 lakeClicked
//     → 主窗口又当作用户点击去驱动地图 → 死循环。
// 所以信号只由"用户点击"（_activate）发出；程序化选中（selectRow）只改
// 高亮/滚动/详情，保持静默。两个入口产生完全相同的视觉结果。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    // 用户点击某行时发出（参数 lakeId）。selectRow 程序化选中**不发**。
    signal lakeClicked(int lakeId)

    // 主窗口联动：地图选中某湖时把它的 lakeId 赋进来，该行变浅色底。
    // 默认 -1 表示无选中。
    property int highlightedId: -1

    // 详情面板当前行（用户点击与 selectRow 都会更新；模型 reset 后失效为 -1）
    property int selectedRow: -1

    readonly property int rowHeight: 34
    readonly property int levelColumn: 6
    // 数值列（面积/周长/NDWI/经度/纬度）右对齐；ID 与等级列不右对齐
    readonly property var numericColumns: [1, 2, 3, 4, 5]
    // 各列的最小宽度（按内容估的基准值）。TableView 会把多余宽度按比例
    // 分摊给这些列，所以 860 宽的表能看到完整的 7 列；窗口变窄时也不会
    // 小于这里给的值——小于就交给横向滚动，而不是把末列裁掉。
    readonly property var columnWidths: [70, 105, 95, 100, 95, 95, 120]
    readonly property int columnsTotal: 680

    // 实际列宽：视口比基准宽就按比例撑开（否则右侧留白、等级色块贴边被裁）。
    function _columnWidth(column) {
        var base = root.columnWidths[column]
        var avail = tableView.width
        if (!(avail > 0) || avail <= root.columnsTotal)
            return base
        return Math.floor(base * avail / root.columnsTotal)
    }

    implicitWidth: 720
    implicitHeight: 480

    function _activate(lakeId) {
        // 用户点击：高亮 + 详情 + 发信号（主窗口据此联动地图）
        selectedRow = app.lakes.row_of(lakeId)
        highlightedId = lakeId
        _refreshDetail()
        root.lakeClicked(lakeId)
    }

    function selectRow(lakeId) {
        // 程序化选中：与用户点击同样的视觉结果，但**不发 lakeClicked**——
        // 防止 map↔table 信号回环（分析见文件头注释）。
        var row = app.lakes.row_of(lakeId)
        if (row < 0)
            return
        highlightedId = lakeId
        selectedRow = row
        _scrollToRow(row)
        _refreshDetail()
    }

    function _scrollToRow(row) {
        tableView.forceLayout()
        // rowHeightProvider 给的是统一行高，row * rowHeight 即该行顶边的偏移
        var top = row * root.rowHeight
        var bottom = top + root.rowHeight
        if (top < tableView.contentY)
            tableView.contentY = top
        else if (bottom > tableView.contentY + tableView.height)
            tableView.contentY = bottom - tableView.height
    }

    function _escapeHtml(text) {
        return String(text)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

    function _refreshDetail() {
        if (selectedRow < 0 || selectedRow >= app.lakes.rowCount()) {
            detailArea.text = "点击行查看判据明细"
            return
        }
        var lines = app.lakes.reasons_at(selectedRow)
        if (!lines || lines.length === 0) {
            detailArea.text = "（该湖暂无判据明细）"
            return
        }
        // 判据文本格式来自 glof.core.risk.Criterion.describe()：
        //   "[命中] …" / "[未命中] …" / "[缺数据未评估] …"
        // 命中/未命中按前缀匹配；"缺数据"按包含匹配（头是"[缺数据未评估]"，
        // 前缀 "[缺数据]" 连 "]" 都对不上）。三色经模型带出（hitColor/missColor/
        // unknownColor，源头是 style.py 的 CRITERION_STATUS_COLORS），这里不写死
        // 色值——改 style 一处，全局同时变。
        var html = ""
        for (var i = 0; i < lines.length; ++i) {
            var line = String(lines[i])
            var color = ""
            if (line.indexOf("[命中]") === 0)
                color = app.lakes.hitColor
            else if (line.indexOf("[未命中]") === 0)
                color = app.lakes.missColor
            else if (line.indexOf("缺数据") !== -1)
                color = app.lakes.unknownColor
            var body = _escapeHtml(line)
            html += (color ? "<span style=\"color:" + color + "\">" + body + "</span>" : body)
            if (i < lines.length - 1)
                html += "<br>"
        }
        detailArea.text = html
    }

    // 供测试/联动定位：第 row 行第 col 列 delegate 中心的窗口坐标。
    // 返回 (-1, -1) 表示该格 delegate 尚未实例化（行在可视区外）。
    function delegateCenter(row, col) {
        tableView.forceLayout()
        var dlg = tableView.itemAtCell(Qt.point(col, row))
        if (!dlg)
            return Qt.point(-1, -1)
        return dlg.mapToItem(null, dlg.width / 2, dlg.height / 2)
    }

    Component.onCompleted: _refreshDetail()

    Connections {
        target: app.lakes
        // set_records()/clear() 会整体 reset：旧行号全部失效，详情退回占位文案
        function onModelReset() {
            root.selectedRow = -1
            root._refreshDetail()
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        HorizontalHeaderView {
            id: header
            Layout.fillWidth: true
            syncView: tableView
        }

        TableView {
            id: tableView
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            model: app.lakes
            // 列宽随视口宽度按比例分摊，HorizontalHeaderView 经 syncView 自动共享
            columnWidthProvider: function(column) { return root._columnWidth(column) }
            rowHeightProvider: function(row) { return root.rowHeight }
            // columnWidthProvider 是函数、不参与属性绑定，宽度变化后必须手动
            // 重排，否则撑开/收窄都只在下一次滚动时才生效。
            onWidthChanged: forceLayout()

            delegate: Rectangle {
                // required 属性由 TableView 注入：model 是角色集，row/column 是格坐标
                required property var model
                required property int row
                required property int column

                implicitHeight: root.rowHeight
                // 选中行浅色底：颜色由主窗口经 highlightedId 驱动
                color: root.highlightedId === model.lakeId ? "#e8f0fe" : "transparent"

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: 1
                    color: "#ececec"
                }

                Text {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 10
                    visible: column !== root.levelColumn
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: root.numericColumns.indexOf(column) >= 0
                                         ? Text.AlignRight : Text.AlignLeft
                    text: model.display
                    font.pixelSize: 12
                    color: "#202124"
                    elide: Text.ElideRight
                }

                // 等级列：圆角色块（color 角色，源头 style.risk_color）+ 白字
                Rectangle {
                    visible: column === root.levelColumn
                    anchors.verticalCenter: parent.verticalCenter
                    x: (parent.width - width) / 2
                    width: chipText.implicitWidth + 18
                    height: parent.height - 12
                    radius: height / 2
                    color: model.color

                    Text {
                        id: chipText
                        anchors.centerIn: parent
                        text: model.display
                        font.pixelSize: 11
                        color: "white"
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root._activate(model.lakeId)
                }
            }
        }

        Frame {
            id: detailFrame
            Layout.fillWidth: true
            Layout.preferredHeight: 148
            padding: 8

            Label {
                id: detailCaption
                anchors.top: parent.top
                anchors.left: parent.left
                text: "判据明细（risk.Criterion.describe 原文）"
                font.pixelSize: 11
                color: "#5f6368"
            }

            TextArea {
                id: detailArea
                objectName: "detailArea"
                anchors.top: detailCaption.bottom
                anchors.topMargin: 4
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                readOnly: true
                textFormat: TextEdit.RichText
                wrapMode: TextArea.Wrap
                font.pixelSize: 12
                text: "点击行查看判据明细"
            }
        }
    }
}
