# GLOF — 帕米尔高原冰湖识别与溃决风险筛查

挑战杯科技发明制作类作品。目标：在帕米尔高原多云多雪条件下，把光学与雷达合成一条
可复现的冰湖自动提取流水线，并输出下游中巴公路（G314）沿线的暴露度清单。

**当前阶段**：QML 主窗口已建成并通过全部冒烟（左参数面板 + 地图 + 时序曲线/风险表），
风险分级贯通 CLI 与时序链路。剩余后补项见文档 §8.3（SAR / 精度评价 / 暴露度 / 报告导出 / 打包）。

---

## 快速开始

```bash
# 1. 依赖（项目内 .venv，不污染系统 Python）
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 八条命令验证整条链路（离线、秒级、可重复）
./.venv/Scripts/python.exe scripts/verify_pipeline.py       # 提取精度：12 条断言
./.venv/Scripts/python.exe scripts/verify_resampling.py     # 逐波段重采样：14 条断言
./.venv/Scripts/python.exe scripts/verify_risk.py           # 风险分级：25 条断言
./.venv/Scripts/python.exe scripts/verify_risk_series.py    # 时序→风险链路：23 条断言
./.venv/Scripts/python.exe scripts/ui_spike.py              # 界面性能 gate：4 条判定
./.venv/Scripts/python.exe scripts/smoke_ui_map.py          # QML 冒烟：地图 26 条
./.venv/Scripts/python.exe scripts/smoke_ui_series.py       # QML 冒烟：时序+线程
./.venv/Scripts/python.exe scripts/smoke_ui_table.py        # QML 冒烟：风险表 35 条
./.venv/Scripts/python.exe scripts/smoke_ui_app.py          # QML 冒烟：主窗口 35 条

# 3. 从 Planetary Computer 抓真实影像（不需要账号），落到本地
./.venv/Scripts/python.exe -m glof.cli fetch \
    --bbox 75.42 38.05 75.62 38.25 --start 2025-07-01 --end 2025-09-30 \
    --max-cloud 5 --save 2

# 4. 对真实影像提取湖泊（抓完就不需要网络了）
./.venv/Scripts/python.exe -m glof.cli extract --input data/raw/<scene>.tif --out output/run1

# 5. 出预览图，肉眼核对提取结果
./.venv/Scripts/python.exe scripts/preview_scene.py --input data/raw/<scene>.tif \
    --out output/preview.png --threshold 0.15

# 6. 从 ICIMOD 的 HMA GLOF 数据库筛出研究区溃决湖
./.venv/Scripts/python.exe -m glof.cli inventory \
    --bbox 74.5 35.4 77.6 38.9 --country China

# 7. 对面积时序逐期做风险分级（留出法估平台、无权重、阈值可覆盖）
./.venv/Scripts/python.exe -m glof.cli risk \
    --input output/kyagar/kyagar_area_series.csv --lake-type ice \
    --sensitivity --out output/risk

# 8. 启动界面（QML 主窗口：左参数面板 + 地图 + 时序曲线/风险表）
./.venv/Scripts/python.exe -m glof.ui --input data/kyagar/S2A_MSIL2A_20250930T054251_R005_T43SGV_20250930T083759.tif
```

> **研究区已定：塔什库尔干县分两层。** 西部帕米尔（75°E）有 4 个湖但零溃决记录；
> 东部喀喇昆仑克勒青河（77°E）有 4 个冰坝湖、38 次溃决记录（Kyagar 一个湖占 34 次）。
> 两块行政上同属塔什库尔干县。详见文档 §7.7。

> **真实数据请用 `--threshold 0.15`，不要用 0。** 实测阈值 0 在真实影像上会把雪斑边缘
> 和河谷湿地全提出来（40 个"水体"，中位 NDWI 只有 0.022）。详见文档 §7.4。

---

## 目录约定

```
glof/
├── core/            纯算法层，禁止 import 任何 Qt / 界面库
│   ├── indices.py   光谱指数 + 波段约定
│   ├── masks.py     冰雪 / 云 / 地形阴影掩膜
│   ├── water.py     阈值 + 形态学 + 矢量化 + 面积统计
│   ├── io.py        栅格读取（逐波段重采样）、落盘
│   ├── stac.py      STAC 检索 + SAS 签名（PC / CDSE 资产名映射）
│   ├── inventory.py ICIMOD HMA GLOF 事件库读取、聚类、按研究区筛选
│   ├── risk.py      分坝型溃决风险分级（可审计规则，无权重）+ 阈值敏感性
│   ├── risk_series.py 时序 → 风险：CSV 读入、留出法参照、逐期判定（CLI `risk` 与界面共用）
│   ├── timeseries.py 时序 API：build_series / 平台面积（口径转发 risk）
│   ├── synthetic.py 合成测试影像（仅测试用）
├── cli.py           无界面批量入口（info / fetch / inventory / extract）
└── ui/              界面层（未创建，只允许调用 core 的公开接口）
scripts/
├── verify_pipeline.py    提取精度验证（9 条断言）
├── verify_resampling.py  逐波段重采样验证（14 条断言）
├── verify_risk.py        风险分级验证（25 条断言，含敏感性分析）
├── verify_risk_series.py 时序→风险链路验证（23 条断言：留出法/年内回退/缺数据诚实记账）
├── ui_spike.py           界面性能 gate（QGraphicsView 拖真实影像，4 条判定）
├── kyagar_series.py      克亚吉尔年际时序（业务参数 + 出图）
├── kyagar_dense.py       2018 溃决前加密时序
└── preview_scene.py      真彩色 + NDWI 预览图，用于肉眼核对
```

**分层是硬约束。** 前期所有验证都走 `cli.py`，界面拖延不会拖死算法主线。

---

## 波段分辨率与重采样

真实 Sentinel-2 L2A 的波段分辨率并不统一：

| 波段 | 原生分辨率 |
|---|---|
| B02 / B03 / B04 / B08 | 10 m |
| **B11 / B12** | **20 m** |
| B01 / B09 / B10 | 60 m |

`io.py` 以最细分辨率的波段为参考网格（S2 是 10 m），其余波段在 `read()` 时用 `out_shape`
一次性重采样过去，不落地中间文件。

```python
# 单文件（合成影像、已对齐的堆栈）——按 spec 里声明的波段号读
scene = load_scene("scene.tif", S2_L2A_6BAND)

# 多文件（真实 S2 L2A，波段分辨率可以不同）
scene = load_scene({
    "green": ("B03.tif", 1),
    "nir":   ("B08.tif", 1),
    "swir1": ("B11.tif", 1),   # 20 m，自动重采样到 10 m
}, S2_L2A_6BAND)

# 想统一到 20 m 分析
scene = load_scene("scene.tif", S2_L2A_6BAND, ref_resolution=20.0)
```

实测（`scripts/verify_resampling.py`）：把 SWIR 拆成独立的 20 m 文件后，
NDWI 与单文件**完全一致**，NDSI 中位差异 0.0049、95 分位 0.0146（雪/湖边界的插值误差），
湖泊数量与面积差异 **0.000%**。

**两个坑已经处理：**

- **几何假设**：各波段同 CRS、同覆盖范围、网格成整数倍嵌套（Sentinel-2 成立）。
  不满足时只做整体缩放、不做几何配准，会抛 `RuntimeWarning` 提示先用 GDAL 对齐。
- **多波段文件只给路径**会默认读第 1 波段。这是最容易犯又最难发现的错误
  （NDWI 算出一堆噪声，只会以为是阈值没调好），所以现在会告警并提示改成 `(路径, 波段号)`。

---

## 三个关键设计决定（都有反例支撑，别改回去）

### 1. 提水用 NDWI，不用 MNDWI

```
MNDWI = (Green - SWIR1) / (Green + SWIR1)     Xu 2006
NDSI  = (Green - SWIR1) / (Green + SWIR1)     Salomonson & Appel 2004
```

**这两个是同一个公式。** 所以"用 MNDWI 提水，再用 NDSI>0.4 掩掉冰雪"在**只按 NDSI 掩膜时**
是自相矛盾的——输出的数值完全一样，掩膜会把水一起删光（实测 F 组：0 个水体）。

雪在 SWIR 反射率极低（0.05–0.15）、Green 很高，MNDWI 会稳定地把雪判成水。
`scripts/verify_pipeline.py` 里有两条断言直接检验这一点。

### 2. 冰雪掩膜必须带 NIR 双条件

只按 NDSI>0.4 掩，**湖泊会整片消失**：

| 地物 | Green | SWIR1 | NDSI |
|---|---|---|---|
| 水体 | 0.055 | 0.008 | **0.75** |
| 雪冰 | 0.880 | 0.100 | 0.80 |

水的 SWIR 太低，NDSI 同样超阈值。区分靠 NIR：雪高反射（0.6–0.9），水强吸收（<0.05）。
这就是 Sen2Cor / Fmask 的做法。

### 3. 填洞口径 = "水体范围"，不是"无冰可见水面"

湖面浮冰会被 NDSI 掩膜剔成一个洞。`fill_holes=True` 会把洞补回来，
因此统计的是水体范围。做精度评价时若要报告"可见水面"，用 `--no-fill-holes`。

---

## 已验证的结论（合成影像）

`scripts/verify_pipeline.py` 实测（9 条断言全部 PASS）：

| 方案 | 水体数 | 总面积 km² | 主湖 km² | 相对误差 |
|---|---|---|---|---|
| A. NDWI + 冰雪掩膜（推荐） | 2 | 0.3036 | 0.2828 | **+0.0%** |
| B. NDWI + 冰雪掩膜，不填洞 | 2 | 0.2984 | 0.2776 | −1.8% |
| C. NDWI，无掩膜 | 3 | 3.1036 | 2.8000 | +890% |
| D. MNDWI，无掩膜 | 3 | 3.1036 | 2.8000 | +890% |
| E. MNDWI + 冰雪掩膜（带 NIR） | 2 | 0.3036 | 0.2828 | +0.0% |
| F. MNDWI + 仅 NDSI 掩膜 | 0 | 0 | — | −100% |

四条结论：

1. **A 组误差 0.00%**，说明链路本身（读栅格 → 指数 → 掩膜 → 矢量化 → 面积）没有系统性偏差。
2. **B 组少 1.8%**，正好是湖面浮冰的面积——填洞不是可选项。
3. **C/D 组虚高 890%**，雪被整片当成水。这一组是"看起来能跑通、结果全错"的典型。
4. **E 组和 F 组是本次最有价值的发现**：MNDWI 加上 NIR 门控后，结果和 A 完全一致（0.3036）；
   而只按 NDSI 掩膜时，F 组一个水体都提不出来——**湖被自己的掩膜删光了**。

也就是说，真正的坑不是"选 NDWI 还是 MNDWI"，而是**冰雪掩膜必须带 NIR 条件**。
NDWI 仍然是首选（HKH 编目标准、对浑浊水体的 SWIR 噪声更钝），
但"掩膜带 NIR"这条比"选哪个指数"更关键。

另一个实测发现：`--threshold otsu` 在这类多雪场景里切出的是 **−0.4458**，
即 land/snow 的分界而不是 water/land 的分界，结果把整片雪判成水。
**自动阈值不能用在有雪的高原场景**，阈值必须显式给。

第五条（重采样，`scripts/verify_resampling.py`）：把 SWIR 拆成独立 20 m 文件后，
NDWI 与单文件完全一致，湖泊面积差异 0.000%，NDSI 中位差异 0.0049。
说明 20 m 的 B11 重采样到 10 m 参考网格这一步没有引入可观测误差。

---

## 下一步

**已完成**：数据链、研究区、判水规则、克亚吉尔时序、风险分级、UI spike（详见文档 §8）。

界面开工的三个 gate 条件已全部满足（文档 §8.2）：

1. **`glof/core/risk.py` 风险分级** ✅ —— 分坝型判据（冰坝：面积/平台比值 0.95，
   用 2017/2018 两个独立年份复现的平台值 1.825 km² 验证；冰碛坝：扩张速率 + 坝体下游
   坡度 + 母冰川接触，缺数据如实标 UNKNOWN）。25 条断言，含阈值 ±20% 敏感性
   （最大翻转率 6.2%，结论不依赖阈值精调）。
2. **`glof/core/timeseries.py` 时序 API** ✅ —— `build_series(bbox, years)`，
   按有效像元数选景；平台面积估算转发 `risk.estimate_plateau_area`，全项目单一口径。
3. **UI spike** ✅ —— 2686×3056 真实场景（8.2 MP）整景解码塞 QGraphicsPixmapItem：
   缩放 P95 8.6 ms、平移 P95 8.0 ms、叠加 200 个多边形 2.9 ms、
   释放影像栈后稳态 165 MB、启动 3.9 s。**结论：不需要按视图窗口分级读取，
   直接整景上。** 证据截图在 `output/ui_spike/`。

**现在开工主窗口**：左参数面板 + 中地图 + 下时序曲线/风险表，**前端改用 QML（Qt Quick）**
（QQuickImageProvider 承接栅格 + Flickable 缩放平移 + Shapes 叠风险多边形 + QtCharts 时序 +
TableView 风险表；整景解码、渲染完丢栈的原则不变）。开发以工作流并行推进：
三个模块 agent 同步开工 → 主窗口集成 → 冒烟/回归终验 → 独立复查 → 修复循环。
可以后补的：SAR 分支、精度评价、暴露度、报告导出（文档 §8.3）。

详细方案与排期见 `docs/选题诊断与修正建议.md`。
