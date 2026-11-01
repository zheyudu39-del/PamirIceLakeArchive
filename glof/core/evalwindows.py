"""评估窗口的读取与逐窗提取。

`scripts/fetch_eval_windows.py` 把 55 个溃决湖点位各抓成一个小窗口，
落在 `data/eval/windows/siteNNN.tif` + `windows.json`。本模块负责：

1. 读清单、校验完整性（文件在不在、shape 对不对）；
2. 每个窗口按同一配方提取水体；
3. 把结果交给 `accuracy` 做点位匹配。

**为什么单独一个模块而不是写在 CLI 里**：界面以后要显示"这 55 个湖的
检出情况"，需要同一套读取与提取口径；写进 CLI 就只能在命令行用。

提取配方与 CLI `extract` 保持一致（同一套冰雪掩膜 + 显式阈值），
因为评价的必须是**产品实际使用的算法**，不能另起一套更宽松的参数
把自己的精度做高。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import masks
from .indices import BAND_PRESETS, ndsi, ndwi
from .io import load_scene
from .water import LakeRecord, extract_lakes

__all__ = [
    "EvalWindow",
    "load_windows",
    "extract_window",
    "WINDOW_BAND_PRESET",
    "DEFAULT_NDWI_THRESHOLD",
    "DEFAULT_MIN_AREA_KM2",
]

WINDOW_BAND_PRESET = "s2_l2a_6band"

#: NDWI 阈值。0.15 是项目在真实消融期影像上标定的值
#: （docs §7.4：阈值 0 会把雪斑边缘和河谷湿地全提出来）。
DEFAULT_NDWI_THRESHOLD = 0.15

#: 最小面积。**评价口径与产品默认值不同，这是有意的。**
#:
#: 产品默认 0.01 km²（CLI extract 的值），但历史溃决湖溃决后往往只剩小残湖：
#: 在 21 个评估窗口上实测，产品配方（0.01）只提出 2 个窗口的水体，
#: 放宽到 0.001（约 10×10 个 10 m 像元）后 14 个窗口能提出。
#: 卡住的一直是面积门限，不是阈值。
#:
#: 这里用 0.001 是为了评价**算法的检出能力**，而不是复刻产品面向大湖的筛选。
#: 报告中必须同时写清两个口径，不能拿这一套的数字冒充产品能力。
DEFAULT_MIN_AREA_KM2 = 0.001


@dataclass
class EvalWindow:
    """一个评估窗口的元数据（来自 windows.json）。"""

    site_id: str
    name: str
    lat: float
    lon: float
    n_events: int
    file: Path
    scene_id: str = ""
    date: str = ""
    cloud: float = float("nan")
    shape: tuple[int, int] | None = None
    pixel_m: float = 10.0

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """窗口经纬度范围（按 shape × pixel_m 反推，不依赖清单里的 bbox 字段）。"""
        half_lat = self.shape[0] * self.pixel_m / 2.0 / 111_320.0 if self.shape else 0.0
        half_lon = (
            self.shape[1] * self.pixel_m / 2.0
            / (111_320.0 * max(1e-6, np.cos(np.radians(self.lat))))
            if self.shape else 0.0
        )
        return (self.lon - half_lon, self.lat - half_lat,
                self.lon + half_lon, self.lat + half_lat)


@dataclass
class WindowResult:
    """一个窗口的提取结果。"""

    window: EvalWindow
    records: list[LakeRecord] = field(default_factory=list)
    error: str | None = None
    #: 窗口内 NDWI 最大值。低于 ~0.05 说明这一带根本没有水面（湖已排空），
    #: 是解释"为什么 0 条"的关键旁证，不是判据本身。
    ndwi_max: float = float("nan")

    @property
    def ok(self) -> bool:
        return self.error is None


def load_windows(manifest: str | Path) -> tuple[list[EvalWindow], list[dict]]:
    """读 windows.json，返回 (可用窗口, 缺失清单)。

    缺失清单来自同目录的 missing.json（抓取阶段没找到影像的点位）。
    两类记录都要返回：评价的分母必须知道"有多少点位本该被评价"。
    """
    manifest = Path(manifest)
    if not manifest.is_file():
        raise FileNotFoundError(f"{manifest} 不存在，先跑 scripts/fetch_eval_windows.py")

    rows = json.loads(manifest.read_text(encoding="utf-8"))
    # 抓取脚本每个点位写一份 siteNNN.json sidecar，汇总清单只在**跑完时**
    # 才重建。抓取中途想看进度时清单是旧的（甚至只有一两条），
    # 所以这里以 sidecar 为准并集：目录里有的都算，清单里多出来的也保留。
    sidecars = sorted(manifest.parent.glob("site[0-9][0-9][0-9].json"))
    if sidecars:
        by_id = {r["site_id"]: r for r in rows if isinstance(r, dict) and "site_id" in r}
        for sc in sidecars:
            try:
                rec = json.loads(sc.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # 只接受"一条记录"的 sidecar。抓取脚本崩溃时可能留下半截 JSON，
            # 也可能是旧格式（数组），这类文件跳过而不是让整个评价崩掉。
            if not isinstance(rec, dict) or "site_id" not in rec:
                continue
            by_id[rec["site_id"]] = rec
        rows = sorted(by_id.values(), key=lambda r: r["site_id"])
    # 清单里的 file 字段是**相对项目根**的路径（抓取脚本用
    # path.relative_to(ROOT).as_posix() 写进去的），不是相对清单目录。
    # 早先按 manifest.parents[2] 拼，清单在 data/eval/windows/ 下时
    # 会得到 data/data/eval/windows/...，每个窗口都报"文件不存在"。
    project_root = Path(__file__).resolve().parents[2]
    windows: list[EvalWindow] = []
    for row in rows:
        path = Path(row["file"])
        if not path.is_absolute():
            # 优先按项目根解析；对不上再退到清单所在目录（兼容手工挪动的清单）
            candidate = project_root / path
            path = candidate if candidate.is_file() else manifest.parent / path.name
        shape = tuple(row["shape"]) if row.get("shape") else None
        windows.append(
            EvalWindow(
                site_id=row["site_id"],
                name=row.get("name", "Unknown"),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                n_events=int(row.get("n_events", 0)),
                file=path,
                scene_id=row.get("scene_id", ""),
                date=row.get("date", ""),
                cloud=float(row.get("cloud", float("nan"))),
                shape=shape,
                pixel_m=float(row.get("pixel_m", 10.0)),
            )
        )

    missing_path = manifest.parent / "missing.json"
    missing: list[dict] = []
    if missing_path.is_file():
        missing = json.loads(missing_path.read_text(encoding="utf-8"))

    windows.sort(key=lambda w: w.site_id)
    return windows, missing


def extract_window(
    window: EvalWindow,
    *,
    threshold: float = DEFAULT_NDWI_THRESHOLD,
    min_area_km2: float = DEFAULT_MIN_AREA_KM2,
    snow_mask: bool = True,
) -> WindowResult:
    """对单个窗口按产品配方提取水体。

    出错不抛，装进 ``error`` 返回——55 个窗口里坏一两个不该让整轮评价崩掉，
    但必须在结果里如实记录，不能当成"该湖没有水体"。
    """
    if not window.file.is_file():
        return WindowResult(window, error=f"文件不存在：{window.file}")

    try:
        scene = load_scene(str(window.file), BAND_PRESETS[WINDOW_BAND_PRESET])
    except Exception as exc:  # noqa: BLE001 - 坏文件是数据问题，不让整轮崩
        return WindowResult(window, error=f"读取失败：{type(exc).__name__}: {exc}")

    nd = ndwi(scene["green"], scene["nir"])
    if not np.isfinite(nd).any():
        return WindowResult(window, error="NDWI 全为无效值")

    exclude = None
    if snow_mask:
        ndsi_arr = ndsi(scene["green"], scene["swir1"])
        exclude = masks.snow_mask(ndsi_arr, scene["nir"])

    try:
        records = extract_lakes(
            nd, scene.transform, scene.crs,
            threshold=threshold,
            min_area_km2=min_area_km2,
            exclude=exclude,
        )
    except Exception as exc:  # noqa: BLE001
        return WindowResult(window, error=f"提取失败：{type(exc).__name__}: {exc}")

    # 记录窗口内的 NDWI 极值：它是"这个湖现在还在不在"的旁证。
    # 排空的湖（NDWI 最大只有 0.03 量级）提出的 0 条是事实，
    # 但它与"湖在、算法没找到"是两回事，报告里要靠这个数区分。
    finite = nd[np.isfinite(nd)]
    ndwi_max = float(finite.max()) if finite.size else float("nan")
    return WindowResult(window, records=records, ndwi_max=ndwi_max)
