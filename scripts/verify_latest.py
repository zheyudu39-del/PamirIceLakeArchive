"""最新观测模块验证：口径一致性 -> 选景顺序 -> 覆盖判据 -> 冰封返回 None -> 实网。

这个模块最容易出的错不是崩溃，是**悄悄给出一个数**：

    * 冰封期提出 0 条湖，于是面积 = 0.0 —— 下游会当成"湖空了"；
    * 判水阈值自己抄一份，跟 timeseries 漂移 —— 当前面积和历史序列不可比；
    * 按云量选景选到瓦片接缝的窄条 —— 面积系统性偏小。

所以下面每一条断言都盯着这三点。默认**不联网**（跑一遍约 1 秒），
加 ``--live`` 才真的去 Planetary Computer 拉一景。

用法：
    ./.venv/Scripts/python.exe scripts/verify_latest.py
    ./.venv/Scripts/python.exe scripts/verify_latest.py --live
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from affine import Affine  # noqa: E402
from pyproj import Transformer  # noqa: E402

from glof.core import latest as glatest  # noqa: E402
from glof.core import stac as gstac  # noqa: E402
from glof.core import timeseries as gts  # noqa: E402
from glof.core import io as gio  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAILURES: list[str] = []
CHECKS = [0]

# 克亚吉尔冰川湖窗口
BBOX = (77.16, 35.65, 77.22, 35.71)
UTM = "EPSG:32643"

# 合成场景必须用 transform_bounds（加密取包络），不能用四角直连：
# 经度边在 UTM 里向外凸，两者宽度差 5.6%，否则覆盖率会算出 0.974 这种
# "看起来像 bug"的数。真实瓦片边界就是包络，这里跟着它走才对得上。
from rasterio.warp import transform_bounds  # noqa: E402

_X0, _Y0, _X1, _Y1 = transform_bounds("EPSG:4326", UTM, *BBOX)
_W_M, _H_M = _X1 - _X0, _Y1 - _Y0


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def make_scene(
    *,
    coverage_scale: float = 2.0,
    green: float = 0.33,
    nir: float = 0.41,
    swir1: float = 0.30,
    water_block: int = 20,
    n: int = 200,
) -> gio.Scene:
    """造一景合成数据。coverage_scale 是相对 bbox 的边长倍数，覆盖比 = 它的平方。

    网格按 bbox 的真实宽高比生成 —— bbox 在 UTM 里不是正方形
    （0.06° 经度 ≈ 5.4 km，0.06° 纬度 ≈ 6.7 km），按正方形造会让
    ``_coverage`` 算出 0.974 这种"看起来像 bug"的数。

    背景默认值取**正常景的岩石/植被**反射率（green≈0.33、nir≈0.41、
    swir1≈0.30，实测 2024-08-14 / 2025-09-13 窗口 NIR 中位数 0.40–0.42），
    只有中间那块 ``water_block`` 是水（nir=0.01）。

    早先的默认值 green=0.06/nir=0.01/swir1=0.008 整幅都是水，加上
    ``MIN_NIR_MEDIAN`` 闸门后会被判成"窗口辐射值异常"直接拒掉——
    那是**对的**（真实世界里整窗是水不可能），是合成数据不真实。
    """
    w_m = _W_M * coverage_scale
    h_m = _H_M * coverage_scale
    nx = n
    ny = max(1, int(round(n * _H_M / _W_M)))
    transform = Affine.translation(_X0, _Y0 + h_m) * Affine.scale(w_m / nx, -h_m / ny)

    # 加一点确定性纹理（固定种子，可复现）。全幅常数会让 2%–98% 百分位拉伸
    # 把整张图拉成黑 —— 那是合成数据不真实，不是拉伸有问题：真实影像有纹理。
    rng = np.random.default_rng(20261102)
    tex = (rng.normal(0.0, 0.02, size=(ny, nx))).astype(np.float32)

    data = {
        "green": np.full((ny, nx), green, dtype=np.float32) + tex,
        "nir": np.full((ny, nx), nir, dtype=np.float32) + tex,
        "swir1": np.full((ny, nx), swir1, dtype=np.float32) + tex,
        # 真彩色：with_image=True 时 fetch_latest 会读 blue/green/red，
        # 界面靠这三个波段渲染影像。合成景必须一并给出，否则测不到显示链路。
        "blue": np.full((ny, nx), 0.25, dtype=np.float32) + tex,
        "red": np.full((ny, nx), 0.30, dtype=np.float32) + tex,
    }
    # 中间放一块"湖"：water_block² 像元，约 (water_block × 27 m)²
    if water_block > 0:
        lo_y = ny // 2 - water_block // 2
        lo_x = nx // 2 - water_block // 2
        for arr, value in (
            ("green", 0.06), ("nir", 0.01), ("swir1", 0.008),
            ("blue", 0.05), ("red", 0.03),
        ):
            data[arr][lo_y:lo_y + water_block, lo_x:lo_x + water_block] = value
    return gio.Scene(
        path="<synthetic>", data=data, transform=transform, crs=UTM,
        reflectance_scale=1.0, target_resolution=10.0,
    )


def make_snow_scene(*, n: int = 200) -> gio.Scene:
    """整窗被雪覆盖的合成场景。

    反射率取实测雪值：green≈0.88、nir≈0.75、swir1≈0.10。
    此时 NDSI≈0.80（>0.4）且 NIR≈0.75（>0.11），雪掩膜占比 = 100%。
    """
    return make_scene(green=0.88, nir=0.75, swir1=0.10, water_block=0, n=n)


def make_noise_scene(*, n: int = 200, seed: int = 0) -> gio.Scene:
    """复现 2026-10-28 那类坏景：0–255 均匀噪声，反射率 0.0001–0.0255。

    实测那一景 B03/B08/B11 中位数都是 0.0128、min/max 恰好 0/255，
    直接跑 NDWI 会得到 36.38 km² 的假湖（窗口总共才约 36 km²）。
    三波段独立采样，所以 NDSI 在 0 附近抖动、雪掩膜占比≈0——
    它只能被 NIR 中位数闸门拦住，这正是这条闸门存在的理由。
    """
    rng = np.random.default_rng(seed)
    ny = max(1, int(round(n * _H_M / _W_M)))
    base = make_scene(water_block=0, n=n)
    data = {
        name: rng.uniform(0.0, 0.0255, size=(ny, n)).astype(np.float32)
        for name in ("green", "nir", "swir1")
    }
    return gio.Scene(
        path="<synthetic-noise>", data=data, transform=base.transform, crs=UTM,
        reflectance_scale=1.0, target_resolution=10.0,
    )


class _FakeSearch:
    """替换 stac.search_sentinel2，返回预先排好序的假场景。"""

    def __init__(self, refs: list[gstac.SceneRef]) -> None:
        self.refs = refs
        self.calls: list[tuple[str, str]] = []

    def __call__(self, bbox, start, end, **kw):  # noqa: ANN001, ANN003
        self.calls.append((start, end))
        return list(self.refs)


def _ref(day: str, cloud: float, tile: str = "T43SFV") -> gstac.SceneRef:
    sid = f"S2B_MSIL2A_{day.replace('-', '')}T000000_R000_T{tile[1:]}_x"
    return gstac.SceneRef(id=sid, datetime=f"{day}T05:00:00Z", cloud_cover=cloud,
                          bbox=BBOX, assets={}, platform="pc")


def with_fakes(refs, scene_for, fn):
    """临时替换检索与读取，跑完恢复。scene_for(ref) 返回 Scene 或抛异常。"""
    orig_search = gstac.search_sentinel2
    orig_sign = gstac.sign_scene
    orig_load = gio.load_scene
    fake = _FakeSearch(refs)
    gstac.search_sentinel2 = fake  # type: ignore[assignment]
    gstac.sign_scene = lambda ref, bands=None, **kw: ({}, {})  # type: ignore[assignment]
    gio.load_scene = lambda *a, **kw: scene_for(a[0] if a else None)  # type: ignore[assignment]
    try:
        return fn()
    finally:
        gstac.search_sentinel2 = orig_search
        gstac.sign_scene = orig_sign
        gio.load_scene = orig_load


# --------------------------------------------------------------------------
def test_thresholds_shared():
    """判水口径必须与 timeseries 是同一批对象，不是抄来的副本。"""
    print("\n[1] 判水口径与 timeseries 一致")
    check("NDWI_THRESHOLD 来自 timeseries", glatest.NDWI_THRESHOLD is gts.NDWI_THRESHOLD,
          f"{glatest.NDWI_THRESHOLD}")
    check("NIR_MAX 来自 timeseries", glatest.NIR_MAX is gts.NIR_MAX, f"{glatest.NIR_MAX}")
    check("SNOW_NDSI_THRESHOLD 来自 timeseries",
          glatest.SNOW_NDSI_THRESHOLD is gts.SNOW_NDSI_THRESHOLD,
          f"{glatest.SNOW_NDSI_THRESHOLD}")
    check("波段组沿用 SERIES_BANDS（3 波段）", glatest.SERIES_BANDS is gts.SERIES_BANDS,
          str(gts.SERIES_BANDS))
    # 值本身也钉住：将来若有人改了 timeseries 的常数，这里会一起响
    check("NDWI 阈值 = 0.02", glatest.NDWI_THRESHOLD == 0.02)
    check("NIR 上限 = 0.20", glatest.NIR_MAX == 0.20)


def test_season():
    print("\n[2] 非消融期判据")
    for month in (6, 7, 8, 9):
        check(f"{month} 月属消融期", glatest._season_warning(date(2026, month, 15)) == "")
    for month in (1, 5, 10, 11, 12):
        msg = glatest._season_warning(date(2026, month, 15))
        check(f"{month} 月给出提示", msg != "", msg[:30])


def test_frozen_is_none_not_zero():
    """冰封/无水信号时面积必须是 None，不能是 0。"""
    print("\n[3] 没量到 → None，不是 0")
    # green == nir -> NDWI = 0，低于 FROZEN_NDWI_MAX
    scene = make_scene(green=0.20, nir=0.20, swir1=0.10, water_block=0)
    obs = with_fakes(
        [_ref("2026-11-01", 3.0)],
        lambda _s: scene,
        lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)),
    )
    check("返回了观测记录", obs is not None)
    assert obs is not None
    check("is_frozen 为 True", obs.is_frozen, f"ndwi_max={obs.ndwi_max:.3f}")
    check("total_area_km2 是 None 而不是 0",
          obs.total_area_km2 is None, repr(obs.total_area_km2))
    check("largest_area_km2 同样是 None", obs.largest_area_km2 is None)
    check("usable 为 False", not obs.usable)
    check("11 月给出季节提示", obs.season_warning != "", obs.season_warning[:40])
    check("没有伪造湖泊记录", obs.n_lakes == 0)


def test_water_detected():
    """有水信号时正常提取，且面积 > 0。"""
    print("\n[4] 消融期正常提取")
    scene = make_scene(coverage_scale=2.0, water_block=20)
    obs = with_fakes(
        [_ref("2026-08-10", 5.0)],
        lambda _s: scene,
        lambda: glatest.fetch_latest(BBOX, today=date(2026, 8, 12)),
    )
    check("返回了观测记录", obs is not None)
    assert obs is not None
    check("不是冰封", not obs.is_frozen, f"ndwi_max={obs.ndwi_max:.3f}")
    check("提取到湖泊", obs.n_lakes >= 1, f"{obs.n_lakes} 条")
    check("面积 > 0", (obs.total_area_km2 or 0) > 0, f"{obs.total_area_km2:.4f} km²")
    check("最大湖面积 = 总面积（只有一个湖）",
          abs((obs.largest_area_km2 or 0) - (obs.total_area_km2 or 0)) < 1e-9)
    check("消融期无季节提示", obs.season_warning == "")
    check("age_days = 2", obs.age_days == 2, f"{obs.age_days}")
    check("观测日期正确", obs.observed_on == "2026-08-10")


def test_no_lake_is_none():
    """有水信号但一条湖也没提出 -> None，不是 0.0。

    实测背景（2026-10-23 T43SFV 那一景）：NDWI 最大值 0.311，过了
    "有水信号"那道闸（>=0.05），但提取 0 条。10 月底湖面已结冰，
    此时写 0.0 会被下游读成"湖干了"——那是替用户断言了一件没观测到的事。
    """
    print("\n[4b] 有水信号但提不出湖 → None，不是 0")
    # 背景陆地正常（NIR 0.41，过质量闸门）。中间放**单个像元**的水信号：
    # NDWI = (0.20-0.10)/(0.20+0.10) ≈ 0.33，远高于 FROZEN_NDWI_MAX(0.05)，
    # 所以走的是"有水信号"分支；但一个像元 ≈ 0.0001 km²，远小于
    # min_area_km2=0.01，提取出来是 0 条。这正是要覆盖的路径：
    # 过了冰封闸、过了质量闸，但确实没提出可信水体。
    scene = make_scene(water_block=0)
    h, w = scene.shape
    scene.data["green"][h // 2, w // 2] = 0.20
    scene.data["nir"][h // 2, w // 2] = 0.10
    obs = with_fakes(
        [_ref("2026-10-23", 1.1)],
        lambda _s: scene,
        lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)),
    )
    check("返回了观测记录", obs is not None)
    assert obs is not None
    check("不是冰封（NDWI 过了第一道闸）", not obs.is_frozen,
          f"ndwi_max={obs.ndwi_max:.3f}")
    check("没有提出湖泊", obs.n_lakes == 0, f"{obs.n_lakes} 条")
    check("total_area_km2 是 None 而不是 0.0",
          obs.total_area_km2 is None, repr(obs.total_area_km2))
    check("largest_area_km2 同样是 None", obs.largest_area_km2 is None)
    check("usable 为 False", not obs.usable)


def test_image_returned():
    """默认必须把影像带回来 —— 用户要的是"看到湖"，只有数字不够。

    这条守的是一个需求偏差：第一版只返回面积数字，界面上没有任何图。
    影像链路缺了 LatestObservation.scene，MapController.set_scene 就拿不到东西。
    """
    print("\n[4c] 默认带回影像，可供界面显示")
    scene = make_scene(coverage_scale=2.0, water_block=20)
    obs = with_fakes(
        [_ref("2026-08-10", 5.0)],
        lambda _s: scene,
        lambda: glatest.fetch_latest(BBOX, today=date(2026, 8, 12)),
    )
    assert obs is not None
    check("scene 不为 None", obs.scene is not None)
    check("ndwi 不为 None", obs.ndwi is not None)
    check("scene 带真彩色波段（red/green/blue）",
          obs.scene.has("red", "green", "blue"),
          str(sorted(obs.scene.data)))
    check("ndwi 形状与 scene 一致", obs.ndwi.shape == obs.scene.shape,
          f"{obs.ndwi.shape} vs {obs.scene.shape}")

    # 真的能渲染成图：这条断言直接把 ui.imagery 的转换跑一遍。
    # 只验数组到 uint8 那一步（QPixmap 需要 QGuiApplication，离线脚本不建）。
    from glof.ui.imagery import stretch_rgb

    rgb = stretch_rgb(obs.scene["red"], obs.scene["green"], obs.scene["blue"])
    check("能拉伸成 uint8 RGB", rgb.dtype == np.uint8 and rgb.shape[2] == 3,
          f"{rgb.dtype} {rgb.shape}")
    check("图不是全黑（拉伸有效）", int(rgb.max()) > 0, f"max={rgb.max()}")

    # with_image=False 时省掉影像，且不多读波段
    obs2 = with_fakes(
        [_ref("2026-08-10", 5.0)],
        lambda _s: scene,
        lambda: glatest.fetch_latest(BBOX, today=date(2026, 8, 12), with_image=False),
    )
    assert obs2 is not None
    check("with_image=False 时不带 scene", obs2.scene is None)
    check("with_image=False 时不带 ndwi", obs2.ndwi is None)
    check("with_image=False 仍能算面积", obs2.total_area_km2 is not None,
          repr(obs2.total_area_km2))


def test_ordering():
    """先比日期新，同日期再比云量低。"""
    print("\n[5] 选景顺序：日期新优先，同日云量低优先")
    scene = make_scene()
    # 旧但干净 vs 新但有云 —— 应该选新的
    refs = [_ref("2026-10-20", 1.0), _ref("2026-10-30", 15.0)]
    obs = with_fakes(refs, lambda _s: scene,
                     lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    assert obs is not None
    check("选更新的那一景（10-30，尽管云更多）",
          obs.observed_on == "2026-10-30", obs.observed_on)

    # 同一天两景，选云量低的
    refs = [_ref("2026-10-30", 15.0, "T43SGV"), _ref("2026-10-30", 2.0, "T43SFV")]
    obs = with_fakes(refs, lambda _s: scene,
                     lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    assert obs is not None
    check("同日期选云量低的", abs(obs.cloud - 2.0) < 1e-9, f"cloud={obs.cloud}")


def test_coverage_gate():
    """覆盖不足的景必须被跳过，且要换到下一个候选。"""
    print("\n[6] 覆盖率门限")
    full = make_scene(coverage_scale=2.0)
    narrow = make_scene(coverage_scale=0.6)   # 覆盖比 ≈ 0.36

    # 新的一景只有窄条，旧的完整 —— 应该落到旧的
    refs = [_ref("2026-10-30", 1.0), _ref("2026-10-28", 3.0)]
    state = {"i": 0}

    def scene_for(_sources):
        i = state["i"]
        state["i"] += 1
        return narrow if i == 0 else full

    obs = with_fakes(refs, scene_for,
                     lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    assert obs is not None
    check("窄条被跳过，用了完整的那景", obs.observed_on == "2026-10-28", obs.observed_on)
    check("覆盖率 ≈ 1.0", obs.coverage > 0.99, f"{obs.coverage:.3f}")

    # 全是窄条 —— 必须返回 None，不能拿窄条凑数
    obs2 = with_fakes([_ref("2026-10-30", 1.0)], lambda _s: narrow,
                      lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    check("全部候选覆盖不足时返回 None", obs2 is None)


def test_quality_gate():
    """云量低但窗口不可用的景必须被拒，且要换到下一个候选。

    这是 2026-11-02 实测 bug 的回归测试：2026-10-28 T43SFV 整景云量 6.5%，
    窗口内全是 0–255 均匀噪声（NIR 中位数 0.0128），NDWI 算出 36.38 km²
    假湖，而窗口总共才约 36 km²。
    """
    print("\n[7] 窗口质量闸门")

    # 闸门常数本身钉住：将来谁改了阈值，这里会一起响
    check("MIN_NIR_MEDIAN = 0.05", glatest.MIN_NIR_MEDIAN == 0.05,
          f"{glatest.MIN_NIR_MEDIAN}")
    check("MAX_SNOW_FRAC = 0.60", glatest.MAX_SNOW_FRAC == 0.60,
          f"{glatest.MAX_SNOW_FRAC}")

    # 先确认合成场景的光谱确实落在预期的两侧，否则下面的"拒/不拒"没有意义
    normal = make_scene()
    snow = make_snow_scene()
    noise = make_noise_scene()
    for label, scene, want_snow in (("正常景", normal, False),
                                    ("雪景", snow, True),
                                    ("噪声景", noise, False)):
        from glof.core.indices import ndsi as _ndsi
        from glof.core import masks as _masks
        mask = _masks.snow_mask(_ndsi(scene["green"], scene["swir1"]), scene["nir"],
                                threshold=gts.SNOW_NDSI_THRESHOLD)
        frac = float(mask.mean())
        nir_med = float(np.median(scene["nir"]))
        check(f"{label}雪掩膜占比{'高' if want_snow else '低'}",
              (frac > glatest.MAX_SNOW_FRAC) if want_snow else (frac <= glatest.MAX_SNOW_FRAC),
              f"{frac:.0%}")
        check(f"{label}NIR 中位数 {'< 0.05' if label == '噪声景' else '>= 0.05'}",
              (nir_med < glatest.MIN_NIR_MEDIAN) if label == "噪声景"
              else (nir_med >= glatest.MIN_NIR_MEDIAN),
              f"{nir_med:.4f}")

    # (a) 新的那景整窗是雪 → 跳过，落到旧的正常景
    refs = [_ref("2026-10-30", 1.0), _ref("2026-10-28", 3.0)]
    state = {"i": 0}
    msgs: list[str] = []

    def scene_for(_sources):
        i = state["i"]
        state["i"] += 1
        return snow if i == 0 else normal

    obs = with_fakes(refs, scene_for,
                     lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2),
                                                  progress=msgs.append))
    check("整窗雪被跳过，用了下一景",
          obs is not None and obs.observed_on == "2026-10-28",
          obs.observed_on if obs else "None")
    check("被拒的那景没有产生观测", state["i"] == 2, f"读了 {state['i']} 景")
    snow_msgs = [m for m in msgs if "积雪覆盖" in m]
    check("progress 说明了积雪拒因并带百分比",
          any("2026-10-30" in m and "%" in m for m in snow_msgs),
          snow_msgs[0] if snow_msgs else "（无）")

    # (b) 全部候选都是雪 → None，且 progress 说明原因
    msgs = []
    obs2 = with_fakes([_ref("2026-10-30", 1.0), _ref("2026-10-28", 3.0)],
                      lambda _s: snow,
                      lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2),
                                                   progress=msgs.append))
    check("全部候选积雪时返回 None", obs2 is None)
    check("progress 说明「均因积雪覆盖被跳过」",
          any("积雪覆盖" in m and "候选" in m for m in msgs),
          next((m for m in msgs if "候选" in m), "（无）"))

    # (c) 噪声景（实测 bug 的复现）→ 被 NIR 闸门拒掉，不能算出假湖
    msgs = []
    obs3 = with_fakes([_ref("2026-10-28", 2.2)], lambda _s: noise,
                      lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2),
                                                   progress=msgs.append))
    check("噪声景返回 None，不再算出 36 km² 假湖", obs3 is None)
    check("progress 说明 NIR 中位数异常",
          any("NIR 中位数" in m for m in msgs),
          next((m for m in msgs if "NIR 中位数" in m), "（无）"))

    # 噪声景在前、正常景在后 —— 必须换到正常景，而不是整体放弃
    state = {"i": 0}

    def noise_then_normal(_sources):
        i = state["i"]
        state["i"] += 1
        return noise if i == 0 else normal

    obs4 = with_fakes([_ref("2026-10-28", 2.2), _ref("2026-10-25", 4.0)],
                      noise_then_normal,
                      lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    check("噪声景后换到正常景",
          obs4 is not None and obs4.observed_on == "2026-10-25",
          obs4.observed_on if obs4 else "None")
    # 合成湖 = 20×20 像元 × (约 54 m)² ≈ 1.2 km²。这里只要证明它是"一块湖"，
    # 而不是噪声景那种整窗 36 km² 的假水
    check("换到的正常景只算出那块合成湖（< 5 km²）",
          obs4 is not None and obs4.total_area_km2 is not None
          and 0 < obs4.total_area_km2 < 5.0,
          f"{obs4.total_area_km2:.4f} km²" if obs4 and obs4.total_area_km2 else "None")

    # (d) 正常合成场景不受影响
    obs5 = with_fakes([_ref("2026-08-10", 5.0)], lambda _s: normal,
                      lambda: glatest.fetch_latest(BBOX, today=date(2026, 8, 12)))
    check("正常合成场景照常出观测",
          obs5 is not None and obs5.total_area_km2 is not None,
          f"{obs5.total_area_km2:.4f} km²" if obs5 and obs5.total_area_km2 else "None")

    # (e) 雪刚好在阈值下方时不能误伤（留出边界的语义）
    from glof.core import masks as _masks
    from glof.core.indices import ndsi as _ndsi
    from glof.core.latest import _window_quality
    reason, nir_med, frac = _window_quality(normal["nir"],
                                            np.zeros(normal.shape, dtype=bool))
    check("正常景通过闸门", reason is None, f"nir_med={nir_med:.3f} snow={frac:.0%}")
    reason2, _, _ = _window_quality(snow["nir"],
                                    _masks.snow_mask(_ndsi(snow["green"], snow["swir1"]),
                                                     snow["nir"],
                                                     threshold=gts.SNOW_NDSI_THRESHOLD))
    check("整窗雪被判为积雪覆盖", reason2 == "积雪覆盖", str(reason2))
    reason3, _, _ = _window_quality(noise["nir"],
                                    _masks.snow_mask(_ndsi(noise["green"], noise["swir1"]),
                                                     noise["nir"],
                                                     threshold=gts.SNOW_NDSI_THRESHOLD))
    check("噪声景被判为辐射值异常", reason3 == "辐射值异常", str(reason3))


def test_no_scenes():
    print("\n[8] 无可用影像")
    obs = with_fakes([], lambda _s: make_scene(),
                     lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    check("检索为空时返回 None", obs is None)

    msgs: list[str] = []
    with_fakes([], lambda _s: make_scene(),
               lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2),
                                            progress=msgs.append))
    check("progress 有输出", len(msgs) > 0, f"{len(msgs)} 条")

    # 检索区间要按 lookback_days 推出来
    fake_refs: list[tuple[str, str]] = []
    orig = gstac.search_sentinel2

    def capture(bbox, start, end, **kw):
        fake_refs.append((start, end))
        return []

    gstac.search_sentinel2 = capture  # type: ignore[assignment]
    try:
        glatest.fetch_latest(BBOX, lookback_days=20, today=date(2026, 11, 2))
    finally:
        gstac.search_sentinel2 = orig
    check("检索区间 = 今天往前 20 天",
          fake_refs and fake_refs[0] == ("2026-10-13", "2026-11-02"), str(fake_refs))


def test_read_failure_fallback():
    """单景读取失败要换下一景，不能整体失败。"""
    print("\n[9] 单景读取失败时回退")
    scene = make_scene()
    refs = [_ref("2026-10-30", 1.0), _ref("2026-10-28", 3.0)]
    state = {"i": 0}

    def scene_for(_sources):
        i = state["i"]
        state["i"] += 1
        if i == 0:
            raise OSError("模拟 504")
        return scene

    obs = with_fakes(refs, scene_for,
                     lambda: glatest.fetch_latest(BBOX, today=date(2026, 11, 2)))
    check("第一景失败后用了第二景",
          obs is not None and obs.observed_on == "2026-10-28",
          obs.observed_on if obs else "None")


def test_cli_wording():
    """CLI 的三种「没量到」措辞必须与实际数字一致。

    这里守的是一个真实的撒谎 bug：实网跑 2026-10-23 那景时，
    NDWI 最大值 0.311 **高于** FROZEN_NDWI_MAX(0.05)，CLI 却打印
    "低于 0.05，说明没有水面信号"。数字和结论互相矛盾，
    用户会以为程序读错了自己的输出。
    """
    import contextlib
    import io

    from glof import cli as gcli

    print("\n[10] CLI 措辞与数字一致")

    def run_cli(obs) -> str:
        """注入一个观测，跑 cmd_latest，抓 stdout。"""
        orig = glatest.fetch_latest
        glatest.fetch_latest = lambda *a, **kw: obs  # type: ignore[assignment]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                gcli.cmd_latest(argparse.Namespace(
                    bbox=list(BBOX), days=60, max_cloud=20.0,
                    min_area_km2=0.01, json=False, save=False, out="output/latest",
                ))
        finally:
            glatest.fetch_latest = orig
        return buf.getvalue()

    def obs_of(**kw):
        base = dict(
            observed_on="2026-10-23", age_days=10, scene_id="S2C_TEST",
            tile="T43SFV", cloud=1.1, coverage=1.0, records=[],
            total_area_km2=None, largest_area_km2=None,
            ndwi_max=float("nan"), season_warning="",
        )
        base.update(kw)
        return glatest.LatestObservation(**base)

    # 1) NDWI 高于门限但没提出湖 —— 不能说"低于 0.05"
    out = run_cli(obs_of(ndwi_max=0.311))
    check("高 NDWI 时不说「低于 0.05」", "低于 0.05" not in out, out.strip().splitlines()[-2].strip())
    check("高 NDWI 时说明「高于」并给出判水口径",
          "高于 0.05" in out and "NDWI>0.02" in out)

    # 2) NDWI 真的低于门限 —— 这才可以说"低于"
    out = run_cli(obs_of(ndwi_max=0.012))
    check("低 NDWI 时说「低于 0.05」并判为无水面信号",
          "低于 0.05" in out and "没有水面信号" in out)

    # 3) NDWI 无效（NaN）
    out = run_cli(obs_of(ndwi_max=float("nan")))
    check("NDWI 无效时标注「无效」而不是编一个数", "无效" in out)

    # 4) 正常量到面积
    out = run_cli(obs_of(total_area_km2=1.83, largest_area_km2=1.20, ndwi_max=0.62))
    check("量到面积时打印 km²", "1.83" in out and "km²" in out)
    check("量到面积时不出现「没量到」", "没量到" not in out)

    # 5) 全程不得把 None 写成 0
    out = run_cli(obs_of(ndwi_max=0.311))
    check("没量到时绝不打印「0.00 km²」", "0.00 km²" not in out)


def test_live():
    print("\n[10] 实网：真的去 Planetary Computer 拉一景")
    today = date.today()
    msgs: list[str] = []
    obs = glatest.fetch_latest(BBOX, lookback_days=60, today=today, progress=msgs.append)
    for m in msgs:
        print(f"      {m}")
    if obs is None:
        print("      （最近 60 天没有可用影像 —— 这本身就是有效结论，不算失败）")
        return
    print(f"      观测日期 {obs.observed_on}（距今 {obs.age_days} 天）"
          f"  云量 {obs.cloud:.1f}%  覆盖 {obs.coverage:.0%}")
    print(f"      瓦片 {obs.tile}  场景 {obs.scene_id[:45]}")
    print(f"      NDWI 最大值 {obs.ndwi_max:.3f}  湖泊 {obs.n_lakes} 个")
    if obs.total_area_km2 is None:
        print("      面积：没量到（无水信号）")
    else:
        print(f"      总面积 {obs.total_area_km2:.4f} km²")
    if obs.season_warning:
        print(f"      提示：{obs.season_warning}")

    check("age_days 与日期一致",
          obs.age_days == (today - date.fromisoformat(obs.observed_on)).days)
    check("age_days 非负", obs.age_days >= 0)
    check("覆盖率达标", obs.coverage >= glatest.MIN_COVERAGE_FRAC, f"{obs.coverage:.3f}")
    check("云量不超过上限", obs.cloud < glatest.DEFAULT_MAX_CLOUD + 1e-6, f"{obs.cloud:.1f}")
    # 面积要么是 None（没量到），要么 > 0 且等于 records 之和。
    # 不允许出现 0.0：窗口内有 NDWI 信号但一条湖都没提出时，也必须是 None
    # ——"没看见"不等于"湖空了"（见 latest.py 的注释）。
    check("面积要么是 None，要么 > 0",
          obs.total_area_km2 is None or obs.total_area_km2 > 0,
          repr(obs.total_area_km2))
    check("面积与 records 之和一致",
          obs.total_area_km2 is None
          or abs(obs.total_area_km2 - sum(r.area_km2 for r in obs.records)) < 1e-9,
          f"{obs.n_lakes} 条 / {obs.total_area_km2}")


def main() -> int:
    live = "--live" in sys.argv
    print("=" * 70)
    print("最新观测模块验证" + ("（含实网）" if live else "（离线）"))
    print("=" * 70)

    test_thresholds_shared()
    test_season()
    test_frozen_is_none_not_zero()
    test_water_detected()
    test_no_lake_is_none()
    test_image_returned()
    test_ordering()
    test_coverage_gate()
    test_quality_gate()
    test_no_scenes()
    test_read_failure_fallback()
    test_cli_wording()
    if live:
        test_live()

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
