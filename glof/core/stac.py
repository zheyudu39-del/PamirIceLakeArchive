"""STAC 检索与资产签名。

这一层的唯一职责：把"我要塔什库尔干 2025 年 7 月云量低于 10% 的影像"
翻译成 `load_scene()` 能直接吃的 ``{波段名: (URL, 波段号)}``。
算法层不需要知道数据来自哪个平台。

**平台差异（会直接 KeyError，别混用）：**

===========  ==========================  ==========================
             Planetary Computer          Copernicus Data Space
===========  ==========================  ==========================
集合 ID       sentinel-2-l2a              sentinel-2-l2a
波段资产名    ``B03`` / ``B11`` / ``SCL``  ``B03_10m`` / ``B11_20m`` / ``SCL_20m``
访问方式      Azure Blob + SAS 签名        S3 或 HTTPS + OIDC Bearer
是否需要账号  不需要                       需要
===========  ==========================  ==========================

本模块默认走 Planetary Computer（不需要账号，签名接口公开）。
CDSE 的资产名映射已实现，但 HTTPS 访问需要 Bearer token，
`sign_scene()` 会要求显式传入，不会去猜。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

__all__ = [
    "PC_ENDPOINT",
    "CDSE_ENDPOINT",
    "COLLECTION",
    "SceneRef",
    "search_sentinel2",
    "sign_scene",
    "clear_sas_cache",
    "BAND_TO_S2",
    "S2_NATIVE_RES",
]

PC_ENDPOINT = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SAS_TEMPLATE = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
CDSE_ENDPOINT = "https://stac.dataspace.copernicus.eu/v1"
COLLECTION = "sentinel-2-l2a"

# 本项目内部波段名 -> Sentinel-2 波段号
BAND_TO_S2 = {
    "blue": "B02",
    "green": "B03",
    "red": "B04",
    "nir": "B08",
    "swir1": "B11",
    "swir2": "B12",
    "scl": "SCL",
}

# 各波段原生分辨率，用于拼 CDSE 的资产名后缀
S2_NATIVE_RES = {
    "B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20,
    "B07": 20, "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20,
    "B12": 20, "SCL": 20, "AOT": 10, "WVP": 10, "TCI": 10,
}

_TILE_RE = re.compile(r"_(T[0-9A-Z]{5})_")


def _asset_key(band: str, platform: str) -> str:
    """把内部波段名翻成平台上的资产键。"""
    s2 = BAND_TO_S2[band]
    if platform == "pc":
        return s2
    if platform == "cdse":
        return f"{s2}_{S2_NATIVE_RES[s2]}m"
    raise ValueError(f"未知平台 {platform!r}，只支持 'pc' / 'cdse'")


def _http_json(url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# ---- SAS token 缓存 -------------------------------------------------------
# Planetary Computer 的 SAS token 对**所有** Sentinel-2 资产通用，有效期约
# 1 小时。原实现每处理一景就重新请求一次：实测单次 1.1 s，25 景就是 27 s
# 纯浪费（见 docs/时序检索为什么慢.md）。
#
# 缓存按 collection 分开（token 的作用域是 collection）。过期时间打七折
# 用——服务端说的 1 小时未必是保守值，提前 30% 过期比拿到 401 再重试便宜。
_SAS_CACHE: dict[str, tuple[str, float]] = {}
_SAS_SAFETY = 0.7


def _sas_token(collection: str = COLLECTION, *, force: bool = False) -> str:
    """取 Planetary Computer 的 SAS token，带进程内缓存。"""
    now = time.monotonic()
    if not force:
        hit = _SAS_CACHE.get(collection)
        if hit is not None and hit[1] > now:
            return hit[0]

    payload = _http_json(PC_SAS_TEMPLATE.format(collection=collection))
    token = payload["token"]
    # 接口给的是 ISO 时间串；解析失败就按 1 小时 × 安全系数兜底，
    # 不去猜格式——这个值只用于决定什么时候重新取，算不准的代价只是多取一次。
    ttl = 3600.0
    expires = payload.get("expiry") or payload.get("expires")
    if expires:
        try:
            dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            ttl = (dt - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            pass
    ttl = max(60.0, min(ttl * _SAS_SAFETY, 3600.0))
    _SAS_CACHE[collection] = (token, now + ttl)
    return token


def clear_sas_cache() -> None:
    """清掉 SAS 缓存（测试用；正常运行不需要调用）。"""
    _SAS_CACHE.clear()


@dataclass
class SceneRef:
    """一景影像的检索结果。"""

    id: str
    datetime: str
    cloud_cover: float
    bbox: tuple[float, float, float, float]
    assets: dict[str, str] = field(default_factory=dict)
    platform: str = "pc"

    @property
    def tile(self) -> str:
        match = _TILE_RE.search(self.id)
        return match.group(1) if match else "-"

    @property
    def date(self) -> str:
        return self.datetime[:10]

    def asset_key(self, band: str) -> str:
        return _asset_key(band, self.platform)

    def sources(self, bands: Sequence[str] = ("blue", "green", "red", "nir", "swir1", "swir2")):
        """转成 ``load_scene()`` 的入参格式：{波段名: (URL, 1)}。"""
        out: dict[str, tuple[str, int]] = {}
        for band in bands:
            key = self.asset_key(band)
            if key not in self.assets:
                raise KeyError(
                    f"{self.id} 没有资产 {key!r}（平台 {self.platform}）。"
                    f" 可用资产示例：{sorted(self.assets)[:8]}"
                )
            out[band] = (self.assets[key], 1)
        return out


def search_sentinel2(
    bbox: Sequence[float],
    start: str,
    end: str,
    *,
    max_cloud: float = 20.0,
    limit: int = 20,
    endpoint: str = PC_ENDPOINT,
    platform: str = "pc",
    max_items: int | None = None,
) -> list[SceneRef]:
    """按范围与时间检索 Sentinel-2 L2A，按云量从低到高返回。

    Parameters
    ----------
    bbox
        ``(lon_min, lat_min, lon_max, lat_max)``，WGS84 经纬度。
    start, end
        ``YYYY-MM-DD``。帕米尔只做 6–9 月消融期，其它月份湖面结冰、NDWI 失效。
    max_cloud
        云量上限（百分比）。注意这是**整景**云量，不是 AOI 内云量——
        整景 0% 的景在 AOI 上仍可能有云，后面要靠 SCL 再筛一次。
    """
    if len(bbox) != 4:
        raise ValueError(f"bbox 需要 4 个数，收到 {len(bbox)} 个")

    payload: dict[str, Any] = {
        "collections": [COLLECTION],
        "bbox": [float(v) for v in bbox],
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": min(limit, 100),
        "query": {"eo:cloud_cover": {"lt": float(max_cloud)}},
    }

    url = f"{endpoint}/search"
    total = limit if max_items is None else max_items
    items: list[dict] = []
    while url and len(items) < total:
        page = _http_json(url, payload)
        items.extend(page.get("features", []))
        payload = None  # 后续页用 next 链接自带的 body / GET 参数
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            break
        if nxt.get("method", "GET").upper() == "POST":
            url, payload = nxt["href"], nxt.get("body")
        else:
            url = nxt["href"]
    items = items[:total]

    refs = []
    for item in items:
        props = item.get("properties", {})
        refs.append(
            SceneRef(
                id=item["id"],
                datetime=props.get("datetime", ""),
                cloud_cover=float(props.get("eo:cloud_cover", float("nan"))),
                bbox=tuple(item.get("bbox", ()))[:4],
                assets={k: v.get("href", "") for k, v in item.get("assets", {}).items()},
                platform=platform,
            )
        )
    refs.sort(key=lambda r: r.cloud_cover)
    return refs


def sign_scene(
    ref: SceneRef,
    bands: Sequence[str] = ("blue", "green", "red", "nir", "swir1", "swir2"),
    *,
    bearer_token: str | None = None,
    collection: str = COLLECTION,
) -> tuple[dict[str, tuple[str, int]], dict[str, str]]:
    """把一景的资产 URL 变成可直接读取的地址。

    Returns
    -------
    (sources, http_headers)
        ``sources`` 直接传给 ``load_scene()``；
        ``http_headers`` 传给 ``load_scene(..., http_headers=...)``，
        Planetary Computer 用 SAS 签名时为空，CDSE 的 Bearer 认证会用到。
    """
    sources = ref.sources(bands)

    if ref.platform == "pc":
        token = _sas_token(collection)
        signed = {
            band: (f"{url}{'&' if '?' in url else '?'}{token}", bidx)
            for band, (url, bidx) in sources.items()
        }
        return signed, {}

    if ref.platform == "cdse":
        if not bearer_token:
            raise ValueError(
                "CDSE 的 HTTPS 访问需要 OIDC Bearer token。\n"
                "  1. 登录 https://dataspace.copernicus.eu/ 后在账号页生成 access token\n"
                "  2. 传给 bearer_token 参数（或设环境变量 CDSE_TOKEN）\n"
                "  备选：直接用 Planetary Computer，不需要任何账号。"
            )
        return sources, {"Authorization": f"Bearer {bearer_token}"}

    raise ValueError(f"未知平台 {ref.platform!r}")
