# 帕米尔冰湖档案

> 给帕米尔高原的每个冰湖建一份**可审计的档案**。
> 冰湖溃决（GLOF）是高原最致命的自然灾害之一，但风险判断长期依赖专家打分：
> 同一个湖，换个人看，结论可能不同，且没人能复算。
> 本项目把这件事拆成可验证的步骤——提取、测面积、按规则判级——
> 每一步的输入、阈值和结果都留痕，任何人可以拿同一景影像复现同一个数。

软件全称（软著申报用）：**帕米尔高原冰湖识别与风险评估软件 V1.0**
许可：MIT（见 `LICENSE`）

---

## 这个系统做什么

输入一景 Sentinel-2 影像，输出一份冰湖清单，每个湖带面积、质心、风险等级
和**逐条判据的实测值**：

```
[命中]  面积/平台比值 = 1.0003（阈值 0.95）：1.8254 km² / 平台 1.8248 km²
```

三件事值得单独说：

**1. 判据是规则，不是权重。** 冰坝湖看「面积逼近平台值的程度」——冰坝湖蓄到
溢流口就不再上涨，所以这个比值有明确物理含义。冰碛坝没有稳定溢流口，
改用扩张速率 + 坝体下游坡度 + 母冰川接触三条。每条判据只控制自己那个开关，
不像权重那样把不同量纲的量混成一个数。

**2. 缺数据就说不知道。** 拿不到 DEM 时，坡度判据输出 `UNKNOWN` 并写明原因，
而不是当成 0 分。有缺口时最高只给 `MEDIUM`，一条没中也给 `UNKNOWN` 而不是
`LOW`——**不把「不知道」当「安全」**。

**3. 阈值可以置疑。** 0.95 这个数不是拍脑袋：`--sensitivity` 会跑 ±20% 的
阈值扰动，报出等级翻转率（实测最大 6.2%），并列出哪几个湖在哪个方向翻转。

## 研究区

塔什库尔干塔吉克自治县（喀什地区）分两块，行政上同属一县：

| 区域 | 经度 | 冰湖 | 溃决记录 |
|---|---|---|---|
| 西部帕米尔（慕士塔格/公格尔） | 75°E | 4 个 | 0 |
| 东部喀喇昆仑克勒青河 | 77°E | 4 个 | 38 次（Kyagar 一湖占 34 次） |

克亚吉尔（Kyagar）冰坝湖 2018-08 的溃决有完整的充水—溃决曲线可验算，
是本项目的主验证案例。

---

## 现在完成到什么程度

**能跑的部分**（13 个验证脚本、514 项断言全部通过）：

| 能力 | 入口 | 状态 |
|---|---|---|
| 影像检索与下载 | `glof.cli fetch` | ✅ Planetary Computer，免账号 |
| 水体提取 + 面积统计 | `glof.cli extract` | ✅ |
| 溃决湖编目查询 | `glof.cli inventory` | ✅ ICIMOD HMA GLOF v4.0 |
| 分坝型风险分级 | `glof.cli risk` | ✅ 含阈值敏感性 |
| 最新可用观测 | `glof.cli latest` | ✅ 拉最近一景，影像上地图 |
| 精度评价（弱真值） | `glof.cli accuracy` | ⚠️ 见下 |
| DEM 坝体下游坡度 | `glof.cli risk --dem` | ⚠️ 见下 |
| **坝体位置自动估计** | `glof.cli risk --dam-position` | ✅ 边界最低点，替掉湖心 |
| **暴露度评估** | `glof.cli exposure` | ⚠️ 需自备矢量数据，见下 |
| **报告导出** | `glof.cli report` | ✅ Markdown + 自包含 HTML |
| 桌面界面 | `python -m glof.ui` | ✅ QML，含地图/时序/风险表 |

**四项需要如实说明的局限**——这几条比任何数字都重要：

- **精度评价**：以 ICIMOD 历史溃决点作弱真值，点位召回率 10.7%（3/28，
  半径 0.15 km）。未命中主因不是算法：18 个窗口提出了水体，其中 14 个距
  点位超过 1 km（中位 1.40 km，最大 3.08 km）。**历史事件坐标与当前残湖的
  几何中心本来就不一致**，0.15 km 的搜索半径够不着这个偏移。所以这个数
  测不了算法检出率，方法不适配，结论已在 `docs/精度评价契约.md` §6 完整记录。
- **DEM 坡度**：用湖心代替坝体位置是错的。实测克亚吉尔湖心四周 500 m
  只有 +10~+250 m 岸坡，量出 -0.01°；真实坡地可达 52°。
  已加保护（`MIN_MEASURABLE_SLOPE_DEG = 1.0`）把近水平结果退回 `UNKNOWN`。
  `--dam-position` 已实现坝体自动估计（边界最低点），合成 DEM 上实测
  把 20° 的真实坡量成 22.74°（偏差 2.7°），而湖心量成 0.56°（偏差 19.4°）。
  **但真实 Kyagar 上仍量出 -0.06°**——它是冰坝湖，坝体是冰川冰，
  DEM 量的是冰面高程，量不出冰川背后的坡。这是数据源限制，不是算法问题。
- **暴露度**：模块与 CLI 都已就位，但**不内置任何地理数据**。
  OSM Overpass 2026-11-02 复测仍不可达（overpass-api.de 返回 502，
  kumi.systems 超时）。编一份 G314 坐标比缺数据危险——缺数据下游会走
  UNKNOWN，编的坐标会被当真值用。要真用需自备道路/居民点 GeoJSON。
- **最新观测的「实时」上限**：Sentinel-2 重访 5 天 + L2A 处理 1–3 天，
  实测 2026-11-02 最新一景是 10-28（距今 5 天）。且近 120 天 64 景里
  云量 <20% 的只有 10 景（16%）。所以叫「最新可用观测」而不是"实时"。

**尚未实现**：SAR 分支（方案中写了双源融合）、PyInstaller 打包。
已实现部分与计划部分的比例见 `docs/选题诊断与修正建议.md` §8。

---

## 快速开始

```bash
# 1. 依赖（项目内 .venv，不污染系统 Python）
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 13 个验证脚本（全部离线、秒级、可重复；合计 514 项断言）
./.venv/Scripts/python.exe scripts/verify_pipeline.py         # 提取精度：12 条
./.venv/Scripts/python.exe scripts/verify_resampling.py       # 逐波段重采样：14 条
./.venv/Scripts/python.exe scripts/verify_risk.py             # 风险分级：25 条
./.venv/Scripts/python.exe scripts/verify_risk_series.py      # 时序→风险：23 条
./.venv/Scripts/python.exe scripts/verify_accuracy.py         # 精度评价：48 条
./.venv/Scripts/python.exe scripts/verify_terrain.py          # DEM 坡度：39 条
./.venv/Scripts/python.exe scripts/verify_dam_position.py     # 坝体位置：43 条
./.venv/Scripts/python.exe scripts/verify_exposure.py         # 暴露度：64 条
./.venv/Scripts/python.exe scripts/verify_report.py           # 报告导出
./.venv/Scripts/python.exe scripts/verify_latest.py           # 最新观测：82 条
./.venv/Scripts/python.exe scripts/verify_series_progress.py  # 时序进度/取消：13 条
./.venv/Scripts/python.exe scripts/verify_cli_wiring.py       # CLI 接线：40 条
./.venv/Scripts/python.exe scripts/verify_event_years.py      # 点位画像：12 条

# 界面冒烟（offscreen，不需要显示器）
./.venv/Scripts/python.exe scripts/smoke_ui_app.py            # 主窗口 55 条
./.venv/Scripts/python.exe scripts/smoke_ui_map.py            # 地图 26 条
./.venv/Scripts/python.exe scripts/smoke_ui_table.py          # 风险表 35 条
./.venv/Scripts/python.exe scripts/smoke_ui_series.py         # 时序 + 线程
./.venv/Scripts/python.exe scripts/smoke_ui_latest.py         # 最新观测 42 条
./.venv/Scripts/python.exe scripts/smoke_ui_filepicker.py     # 选影像对话框 25 条
./.venv/Scripts/python.exe scripts/smoke_ui_lake3d.py         # 3D 湖面回放 50 条

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

# 8. 从 DEM 自动估坝体位置再量下游坡度（替掉"拿湖心当坝体"）
./.venv/Scripts/python.exe -m glof.cli risk \
    --input output/kyagar/kyagar_area_series.csv --lake-type ice \
    --dem "https://copernicus-dem-30m.s3.amazonaws.com/.../Copernicus_DSM_COG_10_N35_00_E077_00_DEM.tif" \
    --dam-position --lakes output/run1/lakes.geojson --out output/risk

# 9. 最新可用观测（拉最近一景，界面里会把影像直接上地图）
./.venv/Scripts/python.exe -m glof.cli latest --bbox 77.16 35.65 77.22 35.71 --days 60

# 10. 暴露度（**需自备矢量数据**，本项目不内置地理数据）
./.venv/Scripts/python.exe -m glof.cli exposure \
    --lakes output/run1/lakes.geojson --roads <道路.geojson> --buffer-km 5

# 11. 汇总成报告（Markdown + 自包含 HTML，无新依赖）
./.venv/Scripts/python.exe -m glof.cli report \
    --lakes output/run1/lakes.geojson --accuracy output/eval/accuracy.json \
    --out output/report

# 12. 启动界面（QML 主窗口：左参数面板 + 地图 + 时序曲线/风险表）
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
│   ├── io.py        栅格读取（逐波段重采样）、落盘 / 读回
│   ├── stac.py      STAC 检索 + SAS 签名（PC / CDSE 资产名映射）
│   ├── inventory.py ICIMOD HMA GLOF 事件库读取、聚类、按研究区筛选
│   ├── risk.py      分坝型溃决风险分级（可审计规则，无权重）+ 阈值敏感性
│   ├── risk_series.py 时序 → 风险：CSV 读入、留出法参照、逐期判定
│   ├── timeseries.py 时序 API：build_series / 平台面积（口径转发 risk）
│   ├── latest.py    最新可用观测：选最近一景、质量闸门、带影像返回
│   ├── accuracy.py  点位匹配、混淆矩阵、球面多边形 IoU（纯几何）
│   ├── terrain.py   DEM 坡度、坝体下游坡度、Copernicus 瓦片读取
│   ├── dam_position.py 坝体位置估计（湖边界最低点），替掉"用湖心当坝体"
│   ├── exposure.py  暴露度：湖到最近道路/居民点的球面距离
│   ├── report.py    报告导出：Markdown + 自包含 HTML（无新依赖）
│   ├── evalwindows.py 评估窗口读取与逐窗提取（口径与产品一致）
│   └── synthetic.py 合成测试影像（仅测试用）
├── cli.py           无界面入口（9 个子命令，见下）
├── ui/              界面层（QML，只允许调用 core 的公开接口）
│   ├── app_state.py 装配与长任务派发
│   ├── worker.py    ExtractWorker / SeriesWorker / LatestWorker（QThread）
│   ├── map_controller.py / lake_model.py / series_controller.py
│   ├── image_provider.py / imagery.py  栅格 → QPixmap
│   └── qml/         Main / MapView / SeriesChart / LakeTable
scripts/
├── verify_pipeline.py       提取精度验证（12 条断言）
├── verify_resampling.py     逐波段重采样验证（14 条断言）
├── verify_risk.py           风险分级验证（25 条断言，含敏感性分析）
├── verify_risk_series.py    时序→风险链路验证（23 条断言）
├── verify_accuracy.py       精度评价模块验证（48 条断言）
├── verify_terrain.py        DEM 坡度验证（39 条断言，含真实 Copernicus 用例）
├── verify_dam_position.py   坝体位置验证（43 条，含"坝体 vs 湖心"对比）
├── verify_exposure.py       暴露度验证（64 条，全合成 GeoJSON）
├── verify_report.py         报告导出验证（含 UNKNOWN/None 措辞）
├── verify_latest.py         最新观测验证（82 条，--live 含实网）
├── verify_series_progress.py 时序进度与取消验证（13 条）
├── verify_cli_wiring.py     CLI 接线验证（40 条，三个新命令的错误路径）
├── verify_event_years.py    ICIMOD 点位画像验证（12 条断言）
├── fetch_eval_windows.py    55 个溃决湖点位 → Sentinel-2 小窗口
├── ui_spike.py              界面性能 gate（4 条判定）
├── smoke_ui_*.py            QML 冒烟（app 55 / map 26 / table 35 / series / latest 42）
├── kyagar_series.py         克亚吉尔年际时序（业务参数 + 出图）
├── kyagar_dense.py          2018 溃决前加密时序
├── preview_scene.py         真彩色 + NDWI 预览图，用于肉眼核对
└── capture_gui.py        截取运行中的窗口（答辩存证用）
```

**分层是硬约束。** `core/` 禁止 import 任何 Qt / 界面库，`ui/` 只调 core 的公开接口。
验证全部走脚本，界面出问题不会拖死算法主线。

---

## 数据（不入库，用脚本重建）

影像和 DEM 体积大，仓库里只放**清单**（记录抽样口径，是复现的前提），
不放大文件。重建方式：

```bash
# 1. 评估数据集：55 个溃决湖点位各取 ±0.03° 小窗（约 120 MB，需联网）
python scripts/fetch_eval_windows.py

# 2. 单景影像：任意 bbox，Planetary Computer 免账号
python -m glof.cli fetch --bbox 77.15 35.60 77.30 35.75 \
    --start 2025-07-01 --end 2025-09-30 --max-cloud 10 --save 2

# 3. DEM：不需要下载，Copernicus GLO-30 支持按 bbox 直接读远程 COG
python -m glof.cli risk --input <series.csv> --lake-type moraine \
    --dem "https://copernicus-dem-30m.s3.amazonaws.com/.../Copernicus_DSM_COG_10_N35_00_E077_00_DEM.tif" \
    --dem-lat 35.677 --dem-lon 77.191

# 4. ICIMOD 溃决事件库：从 ICIMOD 官网申请后放到 data/raw/
#    （Data Download Agreement 要求登记用途，仓库不代为分发）
```

`data/eval/windows/windows.json` 与 `missing.json` **已入库**——
它们记录了每个点位用了哪景影像、什么日期、云量多少、覆盖是否完整，
换台机器重跑脚本会得到同一批影像。

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

**同一路径的波段共用一个 GDAL 句柄。** 像素交错（`interleave=pixel`）的 GeoTIFF
每读一个波段都要解压整块；各波段各开一个句柄时块缓存互不共享，同一块会被解压
N 次。实测 6 波段 spike 场景从 3887 ms 降到 826 ms（`docs` §8.7）。
`load_scene` 内部按 `os.fspath(path)` 去重，关闭时也只关去重后的句柄。

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
   多次复跑区间为缩放 P95 10–12 ms、平移 P95 8–11 ms、叠加 200 个多边形 3–4 ms、
   释放影像栈后稳态 161 MB、启动 2.2–2.5 s（读栈约 1.5–1.7 + 拉伸约 0.7 + QImage 0.02）。
   **结论：不需要按视图窗口分级读取，直接整景上。** 证据截图在 `output/ui_spike/`。
   启动时间原为 5.14–5.18 s，超 5 s gate——根因是逐波段各开 GDAL 句柄、像素交错的块
   被重复解压 6 次；修好后读栈 4.4 s → 1.5 s，详见文档 §8.7。

**现在开工主窗口**：左参数面板 + 中地图 + 下时序曲线/风险表，**前端改用 QML（Qt Quick）**
（QQuickImageProvider 承接栅格 + Flickable 缩放平移 + Shapes 叠风险多边形 + QtCharts 时序 +
TableView 风险表；整景解码、渲染完丢栈的原则不变）。开发以工作流并行推进：
三个模块 agent 同步开工 → 主窗口集成 → 冒烟/回归终验 → 独立复查 → 修复循环。
可以后补的：SAR 分支、精度评价、暴露度、报告导出（文档 §8.3）。

详细方案与排期见 `docs/选题诊断与修正建议.md`。
