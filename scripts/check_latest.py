"""实测：研究区最新可用的 Sentinel-2 影像有多新（数据延迟）。"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(ROOT)

from glof.core import stac as gstac  # noqa: E402

BBOX = (77.16, 35.65, 77.22, 35.71)  # Kyagar

today = date(2026, 11, 2)
print(f"今天是 {today}\n")

# 往前推 120 天，看有多少景、最近的是哪天
start = today - timedelta(days=120)
refs = gstac.search_sentinel2(
    BBOX, start.isoformat(), today.isoformat(), max_cloud=100, limit=100
)
print(f"近 120 天（{start} → {today}）共 {len(refs)} 景")
if refs:
    refs_sorted = sorted(refs, key=lambda r: r.datetime, reverse=True)
    print("\n最近的 10 景（按日期倒序）：")
    for r in refs_sorted[:10]:
        d = r.date
        lag = (today - date.fromisoformat(d)).days
        print(f"  {d}  云量 {r.cloud_cover:5.1f}%  延迟 {lag:3d} 天  {r.id[:40]}")
    newest = date.fromisoformat(refs_sorted[0].date)
    print(f"\n最新一景：{newest}（距今 {(today - newest).days} 天）")

# 按云量筛一下：能用的（云 < 20%）有多新
usable = [r for r in refs if r.cloud_cover < 20]
if usable:
    usable.sort(key=lambda r: r.datetime, reverse=True)
    u = usable[0]
    print(f"云量<20% 的最新一景：{u.date}（距今 {(today - date.fromisoformat(u.date)).days} 天）")
    print(f"  近 120 天里云量<20% 的有 {len(usable)} 景")

# 更长时间窗，看季节规律
print("\n近 3 年每年 7–9 月的可用景数（云<20%）：")
for y in (2023, 2024, 2025):
    rs = gstac.search_sentinel2(BBOX, f"{y}-07-01", f"{y}-09-30", max_cloud=20, limit=100)
    dates = len({r.date for r in rs})
    print(f"  {y}: {len(rs)} 景 / {dates} 个日期")
