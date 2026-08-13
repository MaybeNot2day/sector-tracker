"""PC component price trends scraped from PCPartPicker's public trend pages.

PCPartPicker publishes its price-trend graphs as daily-regenerated PNGs on
an open CDN (they double as the pages' social-embed images); there is no
JSON data endpoint. Each category page carries a JS gallery listing the
current day's chart images with titles ("DDR5-6000 2x32GB"). This service
scrapes those lists, normalizes them into one payload, and caches it for
hours — the source data only changes once a day.

On refresh failure the last good payload keeps serving (stale-on-error):
a broken scrape must never blank the dashboard section.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://pcpartpicker.com"
CACHE_SECONDS = 6 * 3600.0
REQUEST_TIMEOUT_SECONDS = 20.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html",
}

# Display order: memory first — component prices there lead the board's
# MEMORY equity theme (MU, SNDK, DRAM makers).
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("memory", "Memory"),
    ("cpu", "CPUs"),
    ("video-card", "Video Cards"),
    ("internal-hard-drive", "Storage"),
    ("power-supply", "Power Supplies"),
    ("monitor", "Monitors"),
)

# One gallery entry in the page's inline JS:  src: "//cdna...png", ...
# title: "DDR4\u002D3200 2x8GB". src repeats as thumb; parse src+title pairs.
_IMAGE_ENTRY = re.compile(
    r"src:\s*\"(//cdna\.pcpartpicker\.com/[^\"]+/images/trends/[^\"]+\.png)\"[^{}]*?"
    r"title:\s*\"([^\"]+)\"",
    re.DOTALL,
)

_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_lock = asyncio.Lock()


def parse_trend_charts(html: str) -> list[dict[str, str]]:
    """Gallery image list from one trends page; dedupes repeated srcs."""
    charts: list[dict[str, str]] = []
    seen: set[str] = set()
    for src, raw_title in _IMAGE_ENTRY.findall(html):
        if src in seen:
            continue
        seen.add(src)
        # JS string escapes like \u002D arrive literally; decode them.
        title = raw_title.encode("utf-8").decode("unicode_escape").strip()
        charts.append({"title": title, "image": f"https:{src}"})
    return charts


async def _fetch_category(
    client: httpx.AsyncClient, slug: str, label: str
) -> dict[str, object] | None:
    url = f"{BASE_URL}/trends/price/{slug}/"
    try:
        response = await client.get(url, headers=_HEADERS)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("component trends fetch failed for %s", slug)
        return None
    charts = parse_trend_charts(response.text)
    if not charts:
        logger.warning("component trends page for %s carried no charts", slug)
        return None
    return {"slug": slug, "label": label, "url": url, "charts": charts}


async def component_trends_payload() -> dict[str, object]:
    """The /api/component-trends payload, cached for CACHE_SECONDS."""
    async with _lock:
        if _cache["payload"] is not None and monotonic() - float(_cache["at"]) < CACHE_SECONDS:
            return _cache["payload"]  # type: ignore[no-any-return]
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            results = await asyncio.gather(
                *(_fetch_category(client, slug, label) for slug, label in CATEGORIES)
            )
        categories = [category for category in results if category is not None]
        if not categories:
            if _cache["payload"] is not None:
                return _cache["payload"]  # type: ignore[no-any-return]
            return {"as_of": _now_iso(), "source": f"{BASE_URL}/trends/", "categories": []}
        payload: dict[str, object] = {
            "as_of": _now_iso(),
            "source": f"{BASE_URL}/trends/",
            "categories": categories,
        }
        _cache["payload"] = payload
        _cache["at"] = monotonic()
        return payload


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- image proxy -----------------------------------------------------------
# Some browser environments block third-party subresources outright, so the
# frontend loads chart PNGs same-origin through /api/component-image. Only
# the trends CDN prefix is fetchable — this is not an open proxy.
IMAGE_PREFIX = "https://cdna.pcpartpicker.com/static/forever/images/trends/"
_IMAGE_CACHE_MAX = 200
_image_cache: dict[str, tuple[float, bytes, str]] = {}


async def fetch_trend_image(src: str) -> tuple[bytes, str] | None:
    if not src.startswith(IMAGE_PREFIX) or ".." in src:
        return None
    cached = _image_cache.get(src)
    if cached is not None and monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1], cached[2]
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(src, headers=_HEADERS)
            response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("component trend image fetch failed")
        if cached is not None:
            return cached[1], cached[2]
        return None
    content_type = response.headers.get("content-type", "image/png")
    if not content_type.startswith("image/"):
        return None
    if len(_image_cache) >= _IMAGE_CACHE_MAX:
        oldest = min(_image_cache, key=lambda key: _image_cache[key][0])
        del _image_cache[oldest]
    _image_cache[src] = (monotonic(), response.content, content_type)
    return response.content, content_type
