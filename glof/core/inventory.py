"""GLOF 历史事件库：读取、聚类、按研究区筛选。

数据来源
--------
ICIMOD. (2023). *GLOF database of High Mountain Asia* [Data set]. ICIMOD.
DOI: 10.26066/RDS.1973283   （CC BY 4.0，可自由使用，需署名）

压缩包里含 4 个版本（v1.0 2022-12 → v4.0 2025-12）。默认取最新版。
字段里对风险分级直接有用的是：``Lake_type``（冰坝/冰碛坝/滑坡坝）、
``Driver_lake``（母冰川作用）、``Mechanism``、``Elev_lake``、``Repeat``（是否重复溃决）。

为什么需要这一层
----------------
"研究区里有几个已知溃决湖"这个问题，不能靠经纬度框选回答。
先按 bbox 把事件筛出来、再按空间聚类去重，才知道手里到底有几个正样本。
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "GlofEvent",
    "LakeSite",
    "load_glof_database",
    "filter_bbox",
    "cluster_lakes",
    "save_sites_geojson",
    "CITATION",
    "LakeProfile",
    "lake_profile",
    "DEFAULT_DB_PATH",
]

CITATION = (
    "ICIMOD. (2023). GLOF database of High Mountain Asia [Data set]. ICIMOD. "
    "https://doi.org/10.26066/RDS.1973283"
)

_VERSION_RE = re.compile(r"v(\d+)\.(\d+)_(\d{8})\.csv$", re.I)


@dataclass
class GlofEvent:
    gf_id: str
    year: int | None
    month: str
    day: str
    lake_name: str
    lat: float
    lon: float
    elev: float | None
    lake_type: str
    country: str
    province: str
    basin: str
    driver_lake: str
    mechanism: str
    repeat: str
    lives: float | None
    area: float | None

    @property
    def date_label(self) -> str:
        parts = [str(self.year) if self.year else "?"]
        if self.month not in ("", "NA"):
            parts.append(f"{int(float(self.month)):02d}" if self.month.replace('.', '').isdigit() else self.month)
        if self.day not in ("", "NA"):
            parts.append(f"{int(float(self.day)):02d}" if self.day.replace('.', '').isdigit() else self.day)
        return "-".join(parts)


@dataclass
class LakeSite:
    """按空间聚类后的一个溃决湖（同一湖的多次事件合并）。"""

    lat: float
    lon: float
    name: str
    elev: float | None
    lake_type: str
    country: str
    basin: str
    n_events: int
    years: list[int] = field(default_factory=list)
    gf_ids: list[str] = field(default_factory=list)
    total_lives: float = 0.0

    @property
    def first_year(self) -> int | None:
        return min(self.years) if self.years else None

    @property
    def last_year(self) -> int | None:
        return max(self.years) if self.years else None


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in ("NA", "NAN", "NONE"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick_latest_member(names: Iterable[str]) -> str:
    best: tuple[tuple[int, int], str] | None = None
    for name in names:
        match = _VERSION_RE.search(name)
        if not match:
            continue
        key = (int(match.group(1)), int(match.group(2)))
        if best is None or key > best[0]:
            best = (key, name)
    if best is None:
        raise FileNotFoundError("压缩包里没找到 HMAGLOFDB vX.Y_*.csv")
    return best[1]


def load_glof_database(path: str | Path) -> tuple[list[GlofEvent], str]:
    """读取 ICIMOD 的 HMA GLOF 数据库。

    path 可以是解压后的目录、单个 CSV，或原始 zip（自动取最新版本）。
    返回 (事件列表, 使用的文件名)。
    """
    path = Path(path)

    if path.is_dir():
        candidates = sorted(path.rglob("HMAGLOFDB*.csv"))
        if not candidates:
            raise FileNotFoundError(f"{path} 里没有 HMAGLOFDB*.csv")
        member = _pick_latest_member(p.name for p in candidates)
        chosen = next(p for p in candidates if p.name == member)
        text = chosen.read_text(encoding="utf-8-sig", errors="replace")
        source = chosen.name
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            member = _pick_latest_member(zf.namelist())
            text = zf.read(member).decode("utf-8-sig", errors="replace")
        source = Path(member).name
    else:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        source = path.name

    events: list[GlofEvent] = []
    for row in csv.DictReader(io.StringIO(text)):
        lat, lon = _num(row.get("Lat_lake")), _num(row.get("Lon_lake"))
        if lat is None or lon is None:
            continue
        year_val = _num(row.get("Year_exact")) or _num(row.get("Year_approx"))
        events.append(
            GlofEvent(
                gf_id=str(row.get("GF_ID", "")).strip(),
                year=int(year_val) if year_val else None,
                month=str(row.get("Month", "")).strip(),
                day=str(row.get("Day", "")).strip(),
                lake_name=str(row.get("Lake_name", "")).strip(),
                lat=lat,
                lon=lon,
                elev=_num(row.get("Elev_lake")),
                lake_type=str(row.get("Lake_type", "")).strip(),
                country=str(row.get("Country", "")).strip(),
                province=str(row.get("Province", "")).strip(),
                basin=str(row.get("River_Basin", "")).strip(),
                driver_lake=str(row.get("Driver_lake", "")).strip(),
                mechanism=str(row.get("Mechanism", "")).strip(),
                repeat=str(row.get("Repeat", "")).strip(),
                lives=_num(row.get("Lives_total")),
                area=_num(row.get("Area")),
            )
        )
    return events, source


def filter_bbox(
    events: Sequence[GlofEvent],
    bbox: Sequence[float],
    *,
    country: str | None = None,
) -> list[GlofEvent]:
    """bbox = (lon_min, lat_min, lon_max, lat_max)。"""
    west, south, east, north = bbox
    out = [
        e
        for e in events
        if west <= e.lon <= east and south <= e.lat <= north
        and (country is None or e.country.lower() == country.lower())
    ]
    return out


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cluster_lakes(events: Sequence[GlofEvent], radius_m: float = 1000.0) -> list[LakeSite]:
    """把同一坐标附近的多次事件合并成一个湖。

    数据库中同一个湖的多条记录坐标可能有几十米到几百米的差异，
    直接按坐标去重会数出十几个"不同的湖"。这里用空间距离聚类。
    """
    remaining = list(events)
    sites: list[LakeSite] = []

    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        rest: list[GlofEvent] = []
        for ev in remaining:
            if _dist_m(seed.lat, seed.lon, ev.lat, ev.lon) <= radius_m:
                group.append(ev)
            else:
                rest.append(ev)
        remaining = rest

        years = sorted({e.year for e in group if e.year})
        named = [e for e in group if e.lake_name and e.lake_name.upper() not in ("UNKNOWN", "NA")]
        sites.append(
            LakeSite(
                lat=sum(e.lat for e in group) / len(group),
                lon=sum(e.lon for e in group) / len(group),
                name=named[0].lake_name if named else "Unknown",
                elev=next((e.elev for e in group if e.elev), None),
                lake_type=group[0].lake_type,
                country=group[0].country,
                basin=group[0].basin,
                n_events=len(group),
                years=years,
                gf_ids=[e.gf_id for e in group],
                total_lives=sum(e.lives or 0 for e in group),
            )
        )

    sites.sort(key=lambda s: (-s.n_events, s.lat))
    return sites


def save_sites_geojson(sites: Sequence[LakeSite], path: str | Path) -> Path:
    features = []
    for site in sites:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [site.lon, site.lat]},
                "properties": {
                    "name": site.name,
                    "n_events": site.n_events,
                    "first_year": site.first_year,
                    "last_year": site.last_year,
                    "elev_m": site.elev,
                    "lake_type": site.lake_type,
                    "country": site.country,
                    "basin": site.basin,
                    "total_lives": site.total_lives,
                    "source": CITATION,
                },
            }
        )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"type": "FeatureCollection", "features": features},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# 单湖画像：给界面/调用方查"这个点位附近有没有已知溃决湖"（风险接线追加）
# ---------------------------------------------------------------------------

#: 默认事件库：项目根 data/raw 下的 ICIMOD zip（与 scripts/kyagar_series.py
#: 的 --db 缺省值同一份文件）。lake_profile() 的 db 缺省值。
DEFAULT_DB_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "raw" / "GLOF database of High Mountain Asia.zip"
)


@dataclass
class LakeProfile:
    """半径查询命中的单个已知溃决湖画像。

    Attributes
    ----------
    name, lake_type
        库内原文（如 ``"Kyagar"``、``"Ice dammed"``）。注意库的坝型是
        自然语言词表，不是 ``risk.lake_type_kind`` 的别名表——风险判定前
        的归一化由调用方负责（见 glof/ui/app_state.py）。
    n_events
        聚类后该点的溃决事件条数。
    first_year, last_year
        事件年范围；没有带年份的事件时为 None。
    event_years
        升序去重的事件年列表（如克亚吉尔的 1880–2019 共 33 个年份）。
    lat, lon
        聚类质心（该点全部命中事件的均值坐标）。
    """

    name: str
    lake_type: str
    n_events: int
    first_year: int | None
    last_year: int | None
    event_years: list[int]
    lat: float
    lon: float


def lake_profile(
    lat: float,
    lon: float,
    db: str | Path = DEFAULT_DB_PATH,
    radius_deg: float = 0.05,
) -> LakeProfile | None:
    """查一个点位附近的已知溃决湖画像（坝型、事件数、事件年）。

    复用 ``load_glof_database`` + 半径过滤 + ``cluster_lakes``，取事件最多
    的聚类点。半径过滤与 ``scripts/kyagar_series.known_events()`` 同口径：
    ``|Δlat|`` 与 ``|Δlon|`` 都 ≤ radius_deg（0.05° 约 5 km）。

    诚实性约定：邻近没有已知事件、库文件缺失、读取失败一律返回 **None**，
    不抛异常、不猜——调用方（界面）据此如实显示"未知/无记录"。
    """
    try:
        events, _ = load_glof_database(db)
    except Exception:  # noqa: BLE001 - 库缺失/损坏是部署环境问题，不炸调用方
        return None
    near = [
        e for e in events
        if abs(e.lat - lat) <= radius_deg and abs(e.lon - lon) <= radius_deg
    ]
    if not near:
        return None
    sites = cluster_lakes(near, radius_m=1000.0)
    best = max(sites, key=lambda s: s.n_events)
    return LakeProfile(
        name=best.name,
        lake_type=best.lake_type,
        n_events=best.n_events,
        first_year=best.first_year,
        last_year=best.last_year,
        event_years=list(best.years),
        lat=best.lat,
        lon=best.lon,
    )
