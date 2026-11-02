pragma Singleton
import QtQuick

// 全局视觉主题：颜色、间距、字号集中在这里。
//
// 为什么用单例而不是每个文件各写色值：改版前 4 个 QML 里散落着 20+ 个
// 硬编码色值（#202124 / #5f6368 / #e8eaed 各出现十几次），改一处配色
// 要全文搜索替换，且必然漏掉几个。集中之后"深色面板"这类改动是一行的事。
//
// 配色基调：冷灰 + 单一强调色（高原蓝）。风险等级色**不在这里**——
// 它们由 style.py 的 RISK_COLORS 定义并经 MapController 传给 QML，
// 因为那里有"UNKNOWN 绝不能是绿色"的语义约束，必须和算法层同源。

QtObject {
    // ---- 表面 ----
    readonly property color windowBg: "#eef1f5"
    readonly property color panelBg: "#ffffff"
    readonly property color panelBorder: "#dfe3e8"
    readonly property color mapBg: "#0f1216"
    readonly property color footerBg: "#f7f9fb"

    // ---- 文本 ----
    readonly property color textPrimary: "#1a1d21"
    readonly property color textSecondary: "#5b6570"
    readonly property color textMuted: "#8b949e"
    readonly property color textOnAccent: "#ffffff"

    // ---- 强调色（高原蓝） ----
    readonly property color accent: "#2563eb"
    readonly property color accentHover: "#1d4ed8"
    readonly property color accentPressed: "#1e40af"
    readonly property color accentSoft: "#e8f0fe"

    // ---- 语义色 ----
    readonly property color danger: "#c5221f"
    readonly property color dangerSoft: "#fce8e6"
    readonly property color success: "#137333"
    readonly property color successSoft: "#e6f4ea"
    readonly property color warning: "#b06000"
    readonly property color warningSoft: "#fef7e0"

    // ---- 输入控件 ----
    readonly property color fieldBg: "#ffffff"
    readonly property color fieldBorder: "#c8ced6"
    readonly property color fieldBorderFocus: "#2563eb"
    readonly property color divider: "#e6eaef"

    // ---- 间距（4 的倍数，避免 5/7 这类随意值） ----
    readonly property int gapXs: 4
    readonly property int gapSm: 8
    readonly property int gapMd: 12
    readonly property int gapLg: 16
    readonly property int gapXl: 24

    // ---- 圆角 ----
    readonly property int radiusSm: 4
    readonly property int radiusMd: 8
    readonly property int radiusLg: 12

    // ---- 字号 ----
    readonly property int fontCaption: 11
    readonly property int fontBody: 12
    readonly property int fontLabel: 13
    readonly property int fontSection: 14
    readonly property int fontTitle: 17

    // ---- 控件尺寸 ----
    readonly property int controlHeight: 30
    readonly property int panelWidth: 340
}
