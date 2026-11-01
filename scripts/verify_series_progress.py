"""时序检索的进度与取消验证（离线，不联网）。

守的是一个真实的可用性 bug：``build_series`` 原先**只在失败时**调 progress
回调。正常读 200 景期间一声不吭，后果有两条：

    1. 界面上 10 年检索看起来像卡死（实测用户等了 10 分钟以为坏了）；
    2. ``SeriesWorker.cancel()`` 只在 progress 回调进入时检查标志位，
       没有回调点就没有中断点 —— 取消按钮点了没用。

这个脚本用假 STAC 响应 + 假读景，验证正常路径上确实有进度、取消确实生效。
全程不联网，约 1 秒跑完。

用法：
    ./.venv/Scripts/python.exe scripts/verify_series_progress.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from glof.core import io as gio  # noqa: E402
from glof.core import stac as gstac  # noqa: E402
from glof.core import timeseries as gts  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

BBOX = (77.16, 35.65, 77.22, 35.71)


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def _ref(day: str) -> gstac.SceneRef:
    sid = f"S2B_MSIL2A_{day.replace('-', '')}T000000_R000_T43SFV_x"
    return gstac.SceneRef(id=sid, datetime=f"{day}T05:00:00Z", cloud_cover=5.0,
                          bbox=BBOX, assets={}, platform="pc")


def _fake_scene() -> gio.Scene:
    """一小块合成景：中间 10×10 像元是水。"""
    from affine import Affine

    n = 40
    transform = Affine.translation(695400.0, 3954000.0) * Affine.scale(10.0, -10.0)
    green = np.full((n, n), 0.33, dtype=np.float32)
    nir = np.full((n, n), 0.41, dtype=np.float32)
    swir1 = np.full((n, n), 0.30, dtype=np.float32)
    green[15:25, 15:25] = 0.06
    nir[15:25, 15:25] = 0.01
    swir1[15:25, 15:25] = 0.008
    return gio.Scene(path="<fake>", data={"green": green, "nir": nir, "swir1": swir1},
                     transform=transform, crs="EPSG:32643",
                     reflectance_scale=1.0, target_resolution=10.0)


class _Stub:
    """同时顶替检索与读景。"""

    def __init__(self, dates_per_year: dict[int, list[str]]) -> None:
        self.dates = dates_per_year
        self.measure_calls = 0

    def install(self) -> "_Stub":
        self._orig = (gstac.search_sentinel2, gstac.sign_scene, gio.load_scene)
        self._scene = _fake_scene()
        self._stub = self

        def search(bbox, start, end, **kw):
            year = int(start[:4])
            return [_ref(d) for d in self._stub.dates.get(year, [])]

        def sign(ref, bands=None, **kw):
            return {}, {}

        def load(*a, **kw):
            self._stub.measure_calls += 1
            return self._stub._scene

        gstac.search_sentinel2 = search  # type: ignore[assignment]
        gstac.sign_scene = sign  # type: ignore[assignment]
        gio.load_scene = load  # type: ignore[assignment]
        return self

    def restore(self) -> None:
        gstac.search_sentinel2, gstac.sign_scene, gio.load_scene = self._orig


def test_progress_on_success():
    """正常路径上必须报进度 —— 这是取消能生效的前提。"""
    print("\n[1] 正常读数期间有进度回调")
    stub = _Stub({2024: ["2024-07-05", "2024-07-20", "2024-08-01"]}).install()
    msgs: list[str] = []
    try:
        points = gts.build_series(BBOX, [2024], progress=msgs.append)
    finally:
        stub.restore()

    check("三个日期都提出来了", len(points) == 3, f"{len(points)} 点")
    check("progress 被调用过（原先只在失败时调）", len(msgs) > 0, f"{len(msgs)} 条")

    # 每个日期至少一条，才能保证"读完一景就有一个中断点"
    date_msgs = [m for m in msgs if "读取中" in m]
    check("每个日期都报了一次", len(date_msgs) >= 3, f"{len(date_msgs)} 条读取中")

    check("年份进度带序号", any("[1/1]" in m for m in msgs),
          next((m for m in msgs if "[" in m), ""))
    check("进度里带日期", any("2024-07-05" in m for m in msgs))
    check("进度里带当天序号", any("(1/3)" in m for m in msgs))


def test_progress_multi_year():
    print("\n[2] 多年份时年份序号递增")
    stub = _Stub({
        2023: ["2023-07-01"],
        2024: ["2024-07-01"],
        2025: ["2025-07-01"],
    }).install()
    msgs: list[str] = []
    try:
        points = gts.build_series(BBOX, [2023, 2024, 2025], progress=msgs.append)
    finally:
        stub.restore()

    check("三年各一点", len(points) == 3, f"{len(points)}")
    for idx in (1, 2, 3):
        check(f"出现 [{idx}/3] 序号", any(f"[{idx}/3]" in m for m in msgs))


def test_cancel_via_progress():
    """取消的标志位必须在下一个回调点被看到。"""
    print("\n[3] 取消在下一个回调点生效")

    class _Cancelled(Exception):
        pass

    stub = _Stub({2024: ["2024-07-05", "2024-07-20", "2024-08-01"]}).install()
    state = {"cancel": False, "seen": 0}

    def on_progress(msg: str) -> None:
        if state["cancel"]:
            raise _Cancelled()
        state["seen"] += 1
        # 读第二景前请求取消
        if state["seen"] == 3:
            state["cancel"] = True

    raised = False
    try:
        gts.build_series(BBOX, [2024], progress=on_progress)
    except _Cancelled:
        raised = True
    finally:
        stub.restore()

    check("取消真的中断了检索", raised, f"回调 {state['seen']} 次")
    check("取消发生在读完全部日期之前", stub.measure_calls < 3,
          f"只读了 {stub.measure_calls} 景")


def test_no_progress_callback():
    """不传 progress 时不能崩（CLI 与测试都这么用）。"""
    print("\n[4] progress=None 时正常返回")
    stub = _Stub({2024: ["2024-07-05", "2024-07-20"]}).install()
    try:
        points = gts.build_series(BBOX, [2024])
    finally:
        stub.restore()
    check("两个日期两点", len(points) == 2, f"{len(points)}")


def main() -> int:
    print("=" * 70)
    print("时序检索进度与取消验证（离线）")
    print("=" * 70)

    test_progress_on_success()
    test_progress_multi_year()
    test_cancel_via_progress()
    test_no_progress_callback()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"失败 {len(FAILURES)} / {CHECKS[0]}：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"全部通过：{CHECKS[0]} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
