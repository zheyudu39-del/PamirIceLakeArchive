// 3D 湖面回放：DEM 地形 + 按实测面积反演出的水位，时间轴逐期播放。
//
// 这个视图**不模拟溃决过程**。它显示的是每一期实测面积的直接结果：
// 面积是从 Sentinel-2 影像上量出来的（有出处），水位是从 DEM 的水位—面积
// 曲线上反查的（有出处）。没有任何一步是"水往哪流、多快"的模拟。
//
// 为什么值得做 3D：2D 曲线看的是"面积怎么变"，3D 看的是"湖怎么蓄满、溃决后
// 怎么塌"。同一个数据，换一个更贴近物理直觉的表达。数字一个都没变。
//
// 数据来源：上下文属性 app.lake3d（glof.ui.lake3d_controller.Lake3DController）。
//   heights / rows / cols   地形网格（米）
//   frames                  逐期 {date, areaKm2, relativeLevel, absoluteLevel, levelKnown}
//   currentIndex            当前帧
//   levelSource             水位曲线来历（必须显示，不能只画不说）
//   message                 有期次水位算不出来时的提示
//
// 坐标约定：地形网格以中心为原点，X/Z 是水平、Y 是高程。水平方向按
// 像元数等距铺开，垂直方向用真实米数——不放大，避免"湖看起来比实际深"。

import QtQuick
import QtQuick3D
import QtQuick3D.Helpers
import "."

Item {
    id: root

    readonly property bool hasData: app.lake3d ? app.lake3d.ready : false
    readonly property var frames: app.lake3d ? app.lake3d.frames : []
    readonly property int currentIndex: app.lake3d ? app.lake3d.currentIndex : -1
    readonly property var currentFrame:
        (currentIndex >= 0 && currentIndex < frames.length) ? frames[currentIndex] : null

    // 当前帧水位是否可用。不可用时**不画湖面**，而不是画成 0 高度——
    // 0 在这套坐标里是"湖心高程"，画出来会被误解成"湖空了"。
    readonly property bool levelKnown: currentFrame ? currentFrame.levelKnown : false

    // 烘好的山体阴影贴图 URL（image://terrain/...）。空串表示没有，
    // 材质退回纯色——不假装地形有明暗。
    readonly property string textureUrl: app.lake3d ? app.lake3d.textureUrl : ""

    // 地形水平方向的总宽度（场景单位）。96 个顶点铺 400 单位。
    readonly property real terrainSize: 400

    // 垂直压缩系数：地形真实高差可达 2000 m，1:1 铺开会把水平方向压扁。
    // 统一压到 terrainSize 的 0.55，界面上标注"垂直已压缩"，不假装 1:1。
    readonly property real zSpan: hasData ? Math.max(1, app.lake3d.zMax - app.lake3d.zMin) : 1
    readonly property real vScale: terrainSize * 0.55 / zSpan

    // 湖心在场景里的位置。地形网格按 (c/(cols-1) - 0.5) * terrainSize 铺开，
    // 所以归一化坐标 u 对应场景 X = (u - 0.5) * terrainSize，Z 同理。
    readonly property real seedX: app.lake3d ? app.lake3d.seedX : 0.5
    readonly property real seedY: app.lake3d ? app.lake3d.seedY : 0.5
    readonly property real lakeX: (seedX - 0.5) * terrainSize
    readonly property real lakeZ: (seedY - 0.5) * terrainSize

    // 湖心处的地形高程（场景单位）。水面必须从**谷底**起算，不能从场景
    // 原点（原点在 zMin 附近，可能离湖心几百米）。查网格里湖心那一格：
    // 行列由 seedY/seedX 换算。
    readonly property real seedGroundHeight: {
        if (!hasData)
            return 0
        var rows = app.lake3d.rows
        var cols = app.lake3d.cols
        if (rows < 1 || cols < 1)
            return 0
        var r = Math.min(rows - 1, Math.max(0, Math.round(seedY * (rows - 1))))
        var c = Math.min(cols - 1, Math.max(0, Math.round(seedX * (cols - 1))))
        var h = app.lake3d.heights[r * cols + c]
        return (h - app.lake3d.zMin) * vScale
    }

    // 湖盆水位区间（米）——**不是** zSpan。
    //
    // zSpan 是整幅 DEM 的高程差（Kyagar 实测 1981 m，从谷底到山顶），
    // 而湖盆本身的水位只涨 60 m（曲线 4767→4827）。拿 zSpan 去乘相对
    // 水位，等于把 60 m 的水位差按 1981 m 的尺度铺开——实测湖面被抬到
    // y=406，比整座山还高，悬在空中（截图里那块浮空的蓝盘就是这么来的）。
    //
    // 正确做法：用曲线自己的跨度（控制器经 levelSpanM 传过来）。
    readonly property real basinLevelSpan:
        hasData ? Math.max(1, app.lake3d.levelSpanM) : 1

    // 水位视觉放大系数。真实水位差 60 m 在 1981 m 的地形里只占 3%，
    // 画面里不到 7 px，看不出变化（实测三张截图逐像素相同）。放大 3 倍
    // 后占地形高度约 9%，谷深 67 场景单位装得下，不会溢出。
    // **这是视觉夸张**：界面出处条写明"湖面高度已放大 3 倍"，
    // 时间轴上的百分比是未放大的真实值。
    readonly property real waterExaggeration: 3.0

    // 当前帧水面高度（场景单位）= 湖心地面 + 相对水位 × 湖盆水位区间 × 放大。
    // 不可用时返回地面高度（湖面本来 visible=false，不显示）。
    readonly property real waterHeight:
        (hasData && levelKnown)
        ? seedGroundHeight + currentFrame.relativeLevel * basinLevelSpan * vScale * waterExaggeration + 1.0
        : seedGroundHeight

    View3D {
        id: view
        anchors.fill: parent
        visible: root.hasData


        environment: SceneEnvironment {
            clearColor: "#0b1016"
            backgroundMode: SceneEnvironment.Color
            antialiasingMode: SceneEnvironment.NoAA
        }

        // 相机：斜俯视，必须高于地形最高点。
        //
        // 实测教训：把相机压到 y=95（想贴近湖面看清水位变化）会**埋进山体**，
        // 整个视图全黑——地形场景高度 0..220，相机低于 220 就可能在山里。
        // 所以相机固定在 y=430，靠下面的 waterExaggeration 放大水位差，
        // 而不是靠压低相机。
        PerspectiveCamera {
            id: camera
            objectName: "camera3d"
            position: Qt.vector3d(0, 430, 480)
            eulerRotation.x: -38
            fieldOfView: 50
        }

        DirectionalLight {
            eulerRotation.x: -50
            eulerRotation.y: -25
            brightness: 1.1
            castsShadow: false
        }

        // ---- 地形 ----
        Model {
            geometry: ProceduralMesh {
                property var hs: root.hasData ? app.lake3d.heights : []
                property int nRows: root.hasData ? app.lake3d.rows : 0
                property int nCols: root.hasData ? app.lake3d.cols : 0

                // 高程归一化到场景单位：真实高差可达 2000 m，直接铺会把
                // 水平方向压成一条线。压缩系数由根节点的 vScale 统一给，
                // 湖面用同一个值，两者才不会错位。
                readonly property real zMin: root.hasData ? app.lake3d.zMin : 0
                readonly property real vScale: root.vScale

                positions: {
                    var pts = []
                    if (nRows < 2 || nCols < 2)
                        return pts
                    for (var r = 0; r < nRows; ++r)
                        for (var c = 0; c < nCols; ++c)
                            pts.push(Qt.vector3d(
                                (c / (nCols - 1) - 0.5) * root.terrainSize,
                                (hs[r * nCols + c] - zMin) * vScale,
                                (r / (nRows - 1) - 0.5) * root.terrainSize))
                    return pts
                }
                // 地形 UV：贴图是按行列铺的，和 positions 同一套索引顺序。
                uv0s: {
                    var out = []
                    if (nRows < 2 || nCols < 2)
                        return out
                    for (var r = 0; r < nRows; ++r)
                        for (var c = 0; c < nCols; ++c)
                            out.push(Qt.vector2d(c / (nCols - 1), r / (nRows - 1)))
                    return out
                }
                indexes: {
                    var idx = []
                    if (nRows < 2 || nCols < 2)
                        return idx
                    for (var r = 0; r < nRows - 1; ++r)
                        for (var c = 0; c < nCols - 1; ++c) {
                            var a = r * nCols + c
                            var b = a + 1
                            var d = a + nCols
                            var e = d + 1
                            idx.push(a, d, b, b, d, e)
                        }
                    return idx
                }
            }
            materials: PrincipledMaterial {
                // 山体明暗来自烘好的 hillshade 贴图（见 terrain_texture.py）。
                //
                // 为什么不用 normals / 顶点色：实测 Qt 6.12 + software 后端下，
                // ProceduralMesh 的 normals 和 colors 两个属性都存在、赋值
                // 也不报错，但画面逐像素不变。贴图走的是标准 PBR 路径，
                // 软件后端一定支持（probe_3d 里的 Cube 就是靠材质上色的）。
                //
                // 没有纹理时退回纯色，不假装地形有明暗。
                baseColor: "#4a6b52"
                baseColorMap: Texture {
                    source: root.textureUrl
                    generateMipmaps: true
                    // 只设 magFilter。minFilter 的 LinearMipMapLinear 枚举
                    // 在这个 Qt 版本的 Texture 上不存在（实测报
                    // "Unable to assign [undefined] to QQuick3DTexture::Filter"），
                    // 交给默认值，配合 generateMipmaps 已足够。
                    magFilter: Texture.Linear
                }
                roughness: 1.0
                metalness: 0.0
            }
        }

        // ---- 湖面 ----
        // 用一个扁平的圆盘表示水面，高度取当前期反演出的相对水位。
        // 形状是示意（真实湖形是任意多边形），但**高度是实测反推的**。
        // 这一点在界面上要写明，不能让人以为圆盘就是湖的轮廓。
        //
        // 高度换算与地形共用同一套 vScale：地形顶点是
        //   (z - zMin) * vScale
        // 湖面是相对水位 0..1，对应高程 zMin..zMax，所以
        //   relativeLevel * (zMax - zMin) * vScale
        // 两者是同一个式子，只是湖面用比例、地形用绝对高程。
        Model {
            visible: root.hasData && root.levelKnown
            source: "#Cylinder"
            // 圆盘半径按场景比例给。地形铺满 terrainSize=400，湖占其中一小块
            // 谷地。给到 0.5 倍地形宽度（半径约 100 场景单位）是为了**看得见**：
            // 按真实湖宽（约 1.5 km / 地形 11 km ≈ 0.14 倍）画出来只有几十像素，
            // 被两侧山壁挡住后几乎不可见（实测截图里只能看到一个深色小点）。
            // 这是**视觉放大**，界面里写明了"湖面形状是示意"。
            scale: Qt.vector3d(0.75, 0.006, 0.75)
            position: Qt.vector3d(root.lakeX, root.waterHeight, root.lakeZ)
            materials: PrincipledMaterial {
                baseColor: "#2f7fc4"
                opacity: 0.78
                alphaMode: PrincipledMaterial.Blend
                roughness: 0.15
                metalness: 0.1
            }
        }
    }

    // ---- 空态 ----
    Column {
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 420)
        spacing: 10
        visible: !root.hasData

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "3D 湖面回放"
            font.pixelSize: 18
            font.bold: true
            color: Theme.textPrimary
        }
        Text {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            text: "先检索时序（或在湖泊列表选一个湖），这里会用 DEM 地形\n"
                  + "按每期实测面积反演出水位，逐期播放。"
            font.pixelSize: Theme.fontLabel
            color: Theme.textSecondary
            lineHeight: 1.4
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "不模拟溃决过程——只回放实测面积对应的水位"
            font.pixelSize: Theme.fontCaption
            color: Theme.textMuted
        }
    }
}
