"""联网读取的可靠基准：每个配置重复多次取中位数，并测 GDAL 调优选项。

前一版单次测量噪声太大（"2 波段 15.24 s" 比 "3 波段 5.88 s" 还慢，
物理上不可能）。这里每个配置跑 REPEATS 次，报中位数与极差。

同时回答：并发到底有没有用、GDAL 的 HTTP/2 与连接复用能不能省时间。

用法：
    ./.venv/Scripts/python.exe scripts/bench_network.py
"""

from __future__ import annotations

import io
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import rasterio  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import stac as gstac  # noqa: E402
from glof.core.indices import S2_L2A_6BAND  # noqa: E402

BBOX = (77.16, 35.65, 77.22, 35.71)
REPEATS = 3


def bench(label: str, fn, repeats: int = REPEATS) -> float:
    """跑 repeats 次，返回中位数秒数。"""
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<46s} FAIL {type(exc).__name__}: {exc}")
            return float("nan")
        times.append(time.perf_counter() - t0)
    med = statistics.median(times)
    spread = max(times) - min(times)
    print(f"  {label:<46s} {med:6.2f} s  (中位，极差 {spread:.1f})")
    return med


def main() -> int:
    print("=" * 78)
    print(f"联网读取基准（每项 {REPEATS} 次取中位）")
    print("=" * 78)

    refs = gstac.search_sentinel2(
        BBOX, "2025-07-01", "2025-09-15", max_cloud=10, limit=20)
    if not refs:
        print("检索不到影像")
        return 1
    ref = refs[0]
    print(f"\n测试景 {ref.date} {ref.tile}  云量 {ref.cloud_cover:.2f}%")

    gstac._sas_token()  # 预热

    def load(ref, bands, res=None, env=None):
        src, hdr = gstac.sign_scene(ref, bands=bands)
        kwargs = {"bounds": BBOX, "http_headers": hdr or None}
        if res:
            kwargs["ref_resolution"] = res
        if env:
            with rasterio.Env(**env):
                return gio.load_scene(src, S2_L2A_6BAND, **kwargs)
        return gio.load_scene(src, S2_L2A_6BAND, **kwargs)

    print("\n[1] 波段数（10 m）")
    t2 = bench("2 波段 green+nir", lambda: load(ref, ("green", "nir")))
    t3 = bench("3 波段 +swir1（时序口径）", lambda: load(ref, ("green", "nir", "swir1")))
    t5 = bench("5 波段 真彩色（latest 口径）",
               lambda: load(ref, ("blue", "green", "red", "nir", "swir1")))
    print(f"  -> 每加一个波段约 {(t5 - t2) / 3:.2f} s")

    print("\n[2] 分辨率（3 波段）")
    for res in (10.0, 20.0, 40.0):
        bench(f"{res:.0f} m", lambda res=res: load(ref, ("green", "nir", "swir1"), res))

    print("\n[3] GDAL HTTP 调优（3 波段，10 m）")
    base = t3
    for env, name in (
        ({"GDAL_HTTP_VERSION": "2"}, "HTTP/2"),
        ({"GDAL_HTTP_MULTIPLEX": "YES"}, "HTTP/2 multiplex"),
        ({"GDAL_HTTP_VERSION": "2", "GDAL_HTTP_MULTIPLEX": "YES"}, "HTTP/2 + multiplex"),
        ({"GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "1"}, "重试 3 次"),
        ({"VSI_CACHE": "TRUE", "VSI_CACHE_SIZE": "67108864"}, "VSI 缓存 64 MB"),
    ):
        t = bench(name, lambda env=env: load(ref, ("green", "nir", "swir1"), env=env))
        if t == t and base == base:
            print(f"  {'':<46s} {base / t:6.2f}x vs 基线")

    print("\n[4] 并发读多景（3 波段，10 m）—— 关键实验")
    sample = refs[:4]

    def one(r):
        return load(r, ("green", "nir", "swir1"))

    t0 = time.perf_counter()
    for r in sample:
        one(r)
    serial = time.perf_counter() - t0
    print(f"  {'串行 4 景':<46s} {serial:6.2f} s  ({serial / 4:.2f} s/景)")

    for workers in (2, 4, 6):
        best = float("inf")
        for _ in range(2):
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, sample))
            best = min(best, time.perf_counter() - t0)
        print(f"  {f'并发 {workers}':<46s} {best:6.2f} s  "
              f"({best / 4:.2f} s/景，加速 {serial / best:.2f}x)")

    print("\n[5] 只读一个块 vs 整个窗口（B03 单波段）")
    tok = gstac._sas_token()
    url = ref.assets["B03"] + ("&" if "?" in ref.assets["B03"] else "?") + tok

    def read_blocks(n_blocks: int):
        with rasterio.open(url) as ds:
            from rasterio.windows import Window
            bs = ds.block_shapes[0][0]
            for i in range(n_blocks):
                ds.read(1, window=Window(0, i * bs, bs, bs))

    for n in (1, 2, 4, 9):
        bench(f"读 {n} 个 {512}×{512} 块", lambda n=n: read_blocks(n), repeats=2)

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
