"""评估用影像抽样：ICIMOD 溃决湖点位 → 小窗口 Sentinel-2。

为什么按点取窗而不是整景：55 个湖分散在帕米尔/喀喇昆仑，整景要几 GB、
下载以小时计；每个点取 ±PAD_DEG 的小窗只要几十 MB，且正好覆盖
"召回半径"要检验的范围。

产物（落在 data/eval/windows/）：
    siteNNN.tif         6 波段堆栈（10 m，含 B11/B12 重采样）
    siteNNN.json        该窗口的元数据（落盘即写，供断点续跑/清单重建）
    siteNNN.missing.json 无影像点位的显式记录（不静默丢弃）
    windows.json        全部成功窗口的汇总清单
    missing.json        全部无影像点位的汇总清单

**抽样口径必须固定下来**，否则后面算的召回率不可复现：
  * 时间窗：2022–2025 年，每年 7 月 1 日 – 9 月 30 日（消融期，湖面未冻结）
  * 云量上限：20%（**整景**统计，不是 AOI 内云量）
  * 候选排序：整景云量升序，同云量按日期降序 —— 取第一个能读通的
  * 窗口半宽：0.03°（纬向约 3.3 km）——与 app_state.PAD_DEG 同口径

关于窗口尺寸的诚实说明
----------------------
经纬度各 ±0.03° 是**各向异性**的：在 36°N，0.06° 经度 ≈ 5.4 km，
0.06° 纬度 ≈ 6.7 km，所以窗口在 10 m 网格上约 **668 × 542**，
不是 660 × 660。这是沿用 app_state.PAD_DEG 的必然结果，不是 bug；
`--pad` 可改，但评估口径一旦改动就必须同步改文档里的召回率定义。

点位编号
--------
按 (n_events 降序, lat 降序, name, lon 降序) 排序，保证 site001 永远是
同一个湖（cluster_lakes 自带的是 lat 升序，这里显式重排）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from glof.core import io as gio  # noqa: E402
from glof.core import stac as gstac  # noqa: E402
from glof.core.indices import BAND_PRESETS  # noqa: E402
from glof.core.inventory import cluster_lakes, filter_bbox, load_glof_database  # noqa: E402

DB = "data/raw/GLOF database of High Mountain Asia.zip"
OUT = ROOT / "data" / "eval" / "windows"
PAD_DEG = 0.03
BBOX = (73.0, 34.0, 79.0, 39.0)  # (lon_min, lat_min, lon_max, lat_max)
YEARS = (2022, 2023, 2024, 2025)
SEASON = ("07-01", "09-30")
MAX_CLOUD = 20.0
PER_YEAR_LIMIT = 40      # 每年取回多少候选（再按云量排序）
MAX_TRY = 6              # 每个点位最多尝试几个候选景
RETRIES = 4              # 单次网络操作的尝试次数（PC 会间歇 504）
SEARCH_RETRIES = 6       # 检索更便宜，多试几次：一次失败就丢掉整年候选
RETRY_BASE_S = 4.0
MAX_BACKOFF_S = 60.0


# ---------------------------------------------------------------------------
# 网络：Planetary Computer 会间歇性 504 / 超时，必须有退避重试
# ---------------------------------------------------------------------------

def _retry(fn, what: str, attempts: int = RETRIES, base: float = RETRY_BASE_S):
    last: Exception | None = None
    for k in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都值得重试
            last = exc
            if k + 1 < attempts:
                wait = min(base * (2 ** k), MAX_BACKOFF_S)
                print(f"    {what} 失败（{_err_label(exc)}），{wait:.0f}s 后重试", flush=True)
                time.sleep(wait)
    assert last is not None
    raise last


def _err_label(exc: BaseException) -> str:
    """把 HTTPError 的 504/429 之类露出来——只写 'HTTPError' 没法判断是不是限流。"""
    code = getattr(exc, "code", None)
    return f"HTTP {code}" if code else type(exc).__name__


# ---------------------------------------------------------------------------
# 点位
# ---------------------------------------------------------------------------

def pick_sites(bbox=BBOX):
    """研究区内的溃决湖点位，排序固定（编号可复现）。"""
    events, source = load_glof_database(ROOT / DB)
    sub = filter_bbox(events, bbox)
    sites = cluster_lakes(sub)
    sites.sort(key=lambda s: (-s.n_events, -s.lat, s.name, -s.lon))
    return sites, len(events), source


def site_bbox(site, pad: float):
    return (site.lon - pad, site.lat - pad, site.lon + pad, site.lat + pad)


# ---------------------------------------------------------------------------
# 选景：把 4 年候选合到一起排序，避免"逐年取最新"造成口径漂移
# ---------------------------------------------------------------------------

def search_candidates(bbox, max_cloud: float, errors: list[str]):
    candidates = []
    for year in YEARS:
        start = f"{year}-{SEASON[0]}"
        end = f"{year}-{SEASON[1]}"
        try:
            refs = _retry(
                lambda s=start, e=end: gstac.search_sentinel2(
                    bbox, s, e, max_cloud=max_cloud, limit=PER_YEAR_LIMIT),
                f"{year} 检索", attempts=SEARCH_RETRIES,
            )
        except Exception as exc:  # noqa: BLE001 - 单年失败不终止其它年
            errors.append(f"{year}: {_err_label(exc)}")
            continue
        candidates.extend(refs)

    # 去重（同一景可能被相邻月份重复返回）后固定排序：
    # 云量升序 → 日期降序 → id 升序，保证同一批候选的尝试顺序完全确定
    seen: dict[str, gstac.SceneRef] = {}
    for ref in candidates:
        seen.setdefault(ref.id, ref)
    return sorted(
        seen.values(),
        key=lambda r: (r.cloud_cover, -int(r.date.replace("-", "") or 0), r.id),
    )


# ---------------------------------------------------------------------------
# 单点
# ---------------------------------------------------------------------------

def _bounds_alignment(scene, bbox):
    """窗口与请求 bbox 的对齐误差，返回 (实际 WGS84 包围盒, 最大边缘偏移/像元)。

    不能直接把 UTM 窗口的外接经纬度框和请求框相减：UTM 网格相对经纬网有
    约 1° 的收敛角，一个 5.4 km 见方的窗在 WGS84 里是斜的，外接框每边天然
    多出几十米——那是投影，不是错位。

    正确做法：把请求框的四条边投到源 CRS 的米制坐标，与窗口的四条边比，
    差值除以像元大小。这才是"差几个像元"。

    偏移远大于 1 px（且为负向缺失）说明窗口被景边缘裁掉了——`load_scene`
    会把窗口夹到影像范围内，于是留下一个残缺的窗口。调用方据此换景。
    """
    import rasterio
    from rasterio.warp import transform_bounds

    arr = rasterio.transform.array_bounds(scene.shape[0], scene.shape[1], scene.transform)
    actual_wgs = transform_bounds(scene.crs, "EPSG:4326", *arr)
    want_m = transform_bounds("EPSG:4326", scene.crs, *bbox)
    px = scene.pixel_size_m
    # 各边的"缺口"：正值=窗口比请求小（被裁），负值=窗口比请求大（取整膨胀）
    gaps = [(w - a) / px for a, w in zip(arr, want_m)]  # 左、下、右、上
    clipped_px = max(0.0, max(gaps))                     # 真正被裁掉的最大边
    return actual_wgs, max(abs(g) for g in gaps), clipped_px


def _coverage_px(scene, bbox):
    """窗口相对请求 bbox 的覆盖面积比（1.0 = 完整覆盖）。"""
    import rasterio
    from rasterio.warp import transform_bounds

    arr = rasterio.transform.array_bounds(scene.shape[0], scene.shape[1], scene.transform)
    want = transform_bounds("EPSG:4326", scene.crs, *bbox)
    w = min(arr[2], want[2]) - max(arr[0], want[0])
    h = min(arr[3], want[3]) - max(arr[1], want[1])
    full = (want[2] - want[0]) * (want[3] - want[1])
    return max(0.0, w * h) / full if full else 0.0


def fetch_one(site, idx: int, pad: float, max_cloud: float):
    """返回 (record, reason, retryable)。

    retryable=True 表示这次"无影像"可能是网络/服务端抖动造成的，
    下次运行应当重试；False 表示该点位这段时间窗里确实没有可用影像，
    记录下来就不再打扰。区分这两者是评估分母可信的前提。
    """
    site_id = f"site{idx:03d}"
    bbox = site_bbox(site, pad)
    search_errors: list[str] = []
    candidates = search_candidates(bbox, max_cloud, search_errors)

    if not candidates:
        reason = (f"{YEARS[0]}–{YEARS[-1]} 年 {SEASON[0]}–{SEASON[1]} 无云量<{max_cloud:g}% 的影像")
        if search_errors:
            reason += f"；检索失败 {len(search_errors)} 次（{search_errors[-1]}）"
        # 有检索报错说明"查不到"不可信，下次还得再试
        return None, reason, bool(search_errors)

    read_errors: list[str] = []
    # 先读不落盘，检查覆盖；被景边缘裁掉的景留作兜底，不要直接丢掉
    fallback = None
    tried = 0
    for ref in candidates:
        if tried >= MAX_TRY:
            break
        tried += 1
        try:

            sources, headers = _retry(lambda r=ref: gstac.sign_scene(r), "签名")
            scene = _retry(
                lambda s=sources, h=headers: gio.load_scene(
                    s, BAND_PRESETS["s2_l2a_6band"], bounds=bbox, http_headers=h or None),
                f"读取 {ref.id[:20]}",
                attempts=2, base=3.0,
            )
        except Exception as exc:  # noqa: BLE001 - 该景不可用，换下一个候选
            msg = f"{ref.id}: {_err_label(exc)}: {str(exc)[:90]}"
            read_errors.append(msg)
            print(f"    候选不可用 {msg}", flush=True)
            continue

        coverage = _coverage_px(scene, bbox)
        if coverage < 0.98:
            print(f"    候选只覆盖 {coverage:.0%}（景边缘裁切），继续找", flush=True)
            if fallback is None or coverage > fallback[0]:
                fallback = (coverage, ref, scene)
            continue
        return _write_record(site, site_id, ref, scene, bbox,
                             len(candidates), coverage), None, False


    if fallback is not None:
        coverage, ref, scene = fallback
        print(f"    无完整覆盖的景，退而用覆盖 {coverage:.0%} 的最佳窗口", flush=True)
        return (_write_record(site, site_id, ref, scene, bbox,
                              len(candidates), coverage), None, False)

    reason = (f"{tried} 个云量最低的候选景全部读取失败"
              f"（{read_errors[-1] if read_errors else '未知'}）")
    return None, reason, True


def _write_record(site, site_id, ref, scene, bbox, n_candidates, coverage):
    path = OUT / f"{site_id}.tif"
    tmp = path.with_suffix(".tif.part")
    gio.save_stack(scene, tmp)
    os.replace(tmp, path)  # 原子落盘：中断不会留下"存在但损坏"的 tif

    actual, off_px, _clipped_px = _bounds_alignment(scene, bbox)
    # 窗口半宽从 bbox 反推，不另传参数——避免调用方与写盘口径不一致
    pad = (bbox[2] - bbox[0]) / 2.0
    return {
            "site_id": site_id,
            "name": site.name,
            "lat": round(site.lat, 6),
            "lon": round(site.lon, 6),
            "n_events": site.n_events,
            "scene_id": ref.id,
            "date": ref.date,
            "cloud": round(ref.cloud_cover, 2),
            "file": Path(path).relative_to(ROOT).as_posix(),
            "shape": [int(scene.shape[0]), int(scene.shape[1])],
            "pixel_m": round(scene.pixel_size_m, 2),
            "size_mb": round(path.stat().st_size / 1024**2, 2),
            # --- 以下为溯源附加字段 ---
            "tile": ref.tile,
            "years": site.years,
            "lake_type": site.lake_type,
            "country": site.country,
            "elev_m": site.elev,
            "pad_deg": pad,
            "bbox_requested": [round(v, 6) for v in bbox],
            "bbox_actual": [round(v, 6) for v in actual],
            "bounds_offset_px": round(off_px, 3),
            # 覆盖率必须落盘：它是"这个窗口能不能用来评价"的判据。
            # 只写在 fallback 分支会导致正常路径的覆盖率无人可查，
            # 被景边缘裁掉的窗口（实测 site003 只有 0.44）混进清单。
            "coverage": round(coverage, 4),
            "n_candidates": n_candidates,
        }


# ---------------------------------------------------------------------------
# 落盘 / 清单
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def rebuild_manifests() -> tuple[list[dict], list[dict]]:
    """从每个点位的 sidecar 重建 windows.json / missing.json。

    这样即使某次运行在写汇总前中断，下一次也能凭 sidecar 拼出完整清单，
    不会出现"tif 在、清单里没有"的静默丢点。
    """
    windows, missing = [], []
    for p in sorted(OUT.glob("site*.json")):
        name = p.name
        if name.endswith(".missing.json"):
            missing.append(json.loads(p.read_text(encoding="utf-8")))
        else:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if (OUT / Path(rec["file"]).name).is_file():
                windows.append(rec)
            else:
                missing.append({
                    "site_id": rec["site_id"], "name": rec["name"],
                    "lat": rec["lat"], "lon": rec["lon"],
                    "reason": "清单有条目但 tif 已丢失，需重跑",
                })
    windows.sort(key=lambda r: r["site_id"])
    missing.sort(key=lambda r: r["site_id"])
    return windows, missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ICIMOD 溃决湖点位 → Sentinel-2 小窗口")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个点位（冒烟用）")
    ap.add_argument("--pad", type=float, default=PAD_DEG, help=f"窗口半宽（度），默认 {PAD_DEG}")
    ap.add_argument("--max-cloud", type=float, default=MAX_CLOUD, help=f"整景云量上限，默认 {MAX_CLOUD}")
    ap.add_argument("--force", action="store_true", help="忽略已有文件，全部重取")
    args = ap.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    sites, n_all_events, source = pick_sites()
    print(f"事件库 {source}：全库 {n_all_events} 条，研究区 {BBOX} 内聚成 {len(sites)} 个湖")
    print(f"抽样口径：{YEARS[0]}–{YEARS[-1]} 年 {SEASON[0]}–{SEASON[1]}，"
          f"整景云量<{args.max_cloud:g}%，窗口半宽 ±{args.pad}°")
    print(f"输出 {OUT}\n", flush=True)

    todo = sites[: args.limit] if args.limit else sites
    t0 = time.time()
    for i, site in enumerate(todo, start=1):
        site_id = f"site{i:03d}"
        tif = OUT / f"{site_id}.tif"
        meta = OUT / f"{site_id}.json"
        miss = OUT / f"{site_id}.missing.json"
        if not args.force:
            if tif.is_file() and meta.is_file():
                print(f"[{i:2d}/{len(todo)}] {site.name[:18]:20s} 已存在，跳过")
                continue
            # 确定性的"无影像"已记录过就不再重试；因网络失败记下的要重试
            if miss.is_file() and not json.loads(miss.read_text(encoding="utf-8")).get("retryable"):
                print(f"[{i:2d}/{len(todo)}] {site.name[:18]:20s} 已确认无影像，跳过")
                continue

        print(f"[{i:2d}/{len(todo)}] {site.name[:18]:20s} "
              f"{site.lat:.4f},{site.lon:.4f} 事件{site.n_events}", flush=True)
        rec, reason, retryable = fetch_one(site, i, args.pad, args.max_cloud)

        if rec is None:
            print(f"    无可用影像：{reason}", flush=True)
            _write_json(OUT / f"{site_id}.missing.json", {
                "site_id": site_id,
                "name": site.name,
                "lat": round(site.lat, 6),
                "lon": round(site.lon, 6),
                "reason": reason,
                "retryable": retryable,
            })
            (OUT / f"{site_id}.json").unlink(missing_ok=True)
            continue

        _write_json(meta, rec)
        (OUT / f"{site_id}.missing.json").unlink(missing_ok=True)
        print(f"    OK {rec['scene_id'][:44]} {rec['date']} "
              f"云{rec['cloud']}% {rec['shape'][1]}×{rec['shape'][0]}px "
              f"{rec['size_mb']}MB 偏移{rec['bounds_offset_px']}px", flush=True)

    windows, missing = rebuild_manifests()
    _write_json(OUT / "windows.json", windows)
    _write_json(OUT / "missing.json", missing)

    elapsed = time.time() - t0
    total_mb = sum(r["size_mb"] for r in windows)
    print("\n" + "=" * 62)
    print(f"汇总：成功 {len(windows)} / 无影像 {len(missing)} / 点位 {len(sites)}")
    print(f"总体积 {total_mb:.1f} MB，本次耗时 {elapsed:.0f}s")
    by_id = {f"site{i:03d}": s for i, s in enumerate(sites, start=1)}
    if windows:
        shapes = [tuple(r["shape"]) for r in windows]
        hs = sorted(h[0] for h in shapes)
        ws = sorted(h[1] for h in shapes)
        print(f"窗口行数 min/median/max = {hs[0]} / {statistics.median(hs):.0f} / {hs[-1]}")
        print(f"窗口列数 min/median/max = {ws[0]} / {statistics.median(ws):.0f} / {ws[-1]}")
        off = max(r["bounds_offset_px"] for r in windows)
        print(f"bounds 最大偏移 {off:.2f} px（要求 <1 px）")
        # 尺寸异常（被景边缘裁切）单独点名
        ref_h = statistics.median(hs)
        ref_w = statistics.median(ws)
        odd = [r for r in windows
               if abs(r["shape"][0] - ref_h) > 40 or abs(r["shape"][1] - ref_w) > 40]
        if odd:
            print("尺寸明显偏离中位数的窗口（可能被景边缘裁切）：")
            for r in odd:
                print(f"  {r['site_id']} {r['name'][:16]:18s} {r['shape'][1]}×{r['shape'][0]} "
                      f"{r['tile']} {r['date']}")
        # 覆盖率不足的窗口不能进评价集：它的"没检出水体"可能只是没拍到
        low = [r for r in windows if r.get("coverage", 1.0) < 0.98]
        if low:
            print(f"\n警告：{len(low)} 个窗口覆盖不足 98%，已从评价集排除：")
            for r in low:
                print(f"  {r['site_id']} {r['name'][:16]:18s} 覆盖 {r.get('coverage')}")
    if missing:
        print("无影像点位：")
        for m in missing:
            flag = "（可重试）" if m.get("retryable") else ""
            print(f"  {m['site_id']} {m['name'][:16]:18s} {m['lat']:.4f},{m['lon']:.4f} "
                  f"— {m['reason']}{flag}")
    print(f"清单 {OUT / 'windows.json'} / {OUT / 'missing.json'}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
