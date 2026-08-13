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
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
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


async def component_trends_payload(store_path: Path | None = None) -> dict[str, object]:
    """The /api/component-trends payload.

    Priority: fresh in-memory cache, fresh pushed store (datacenter hosts
    cannot scrape PCPartPicker — Cloudflare 403s their IP ranges, so a
    residential-network pusher POSTs the payload in), live scrape (works in
    local development), then any stale fallback rather than an empty rail.
    """
    async with _lock:
        if _cache["payload"] is not None and monotonic() - float(_cache["at"]) < CACHE_SECONDS:
            return _cache["payload"]  # type: ignore[no-any-return]
        pushed = _load_store(store_path)
        if pushed is not None and _payload_age_seconds(pushed) < PUSH_FRESH_SECONDS:
            _cache["payload"] = pushed
            _cache["at"] = monotonic()
            return pushed
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            results = await asyncio.gather(
                *(_fetch_category(client, slug, label) for slug, label in CATEGORIES)
            )
        categories = [category for category in results if category is not None]
        if not categories:
            for fallback in (pushed, _cache["payload"]):
                if fallback is not None:
                    return fallback
            return {"as_of": _now_iso(), "source": f"{BASE_URL}/trends/", "categories": []}
        payload: dict[str, object] = {
            "as_of": _now_iso(),
            "source": f"{BASE_URL}/trends/",
            "categories": categories,
        }
        _cache["payload"] = payload
        _cache["at"] = monotonic()
        return payload


# --- pushed store ----------------------------------------------------------
# A machine whose egress Cloudflare tolerates runs
# scripts/component_trends_pusher.py and POSTs the scraped payload here.
STORE_BASENAME = "component_trends.json"
PUSH_FRESH_SECONDS = 72 * 3600.0
_MAX_CATEGORIES = 12
_MAX_CHARTS = 60


def store_file(database_path: Path) -> Path:
    return database_path.parent / STORE_BASENAME


def normalize_pushed_payload(raw: Mapping[str, Any]) -> dict[str, object] | None:
    """Validated, normalized copy of a pushed payload; None when malformed."""
    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, list) or not 1 <= len(raw_categories) <= _MAX_CATEGORIES:
        return None
    categories: list[dict[str, object]] = []
    for entry in raw_categories:
        if not isinstance(entry, Mapping):
            return None
        slug = str(entry.get("slug") or "").strip()
        label = str(entry.get("label") or "").strip()
        url = str(entry.get("url") or "").strip()
        raw_charts = entry.get("charts")
        if (
            not re.fullmatch(r"[a-z0-9-]{2,40}", slug)
            or not label
            or len(label) > 40
            or not url.startswith(f"{BASE_URL}/")
            or not isinstance(raw_charts, list)
            or not 1 <= len(raw_charts) <= _MAX_CHARTS
        ):
            return None
        charts: list[dict[str, str]] = []
        for chart in raw_charts:
            if not isinstance(chart, Mapping):
                return None
            title = str(chart.get("title") or "").strip()
            image = str(chart.get("image") or "").strip()
            if not title or len(title) > 120 or not image.startswith(IMAGE_PREFIX):
                return None
            charts.append({"title": title, "image": image})
        categories.append({"slug": slug, "label": label, "url": url, "charts": charts})
    return {"as_of": _now_iso(), "source": f"{BASE_URL}/trends/", "categories": categories}


def save_pushed_payload(store_path: Path, payload: dict[str, object]) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    temp = store_path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(store_path)
    # The next GET must serve the fresh push, not yesterday's memory cache.
    _cache["payload"] = payload
    _cache["at"] = monotonic()


def _load_store(store_path: Path | None) -> dict[str, object] | None:
    if store_path is None:
        return None
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("categories"), list) else None


def _payload_age_seconds(payload: Mapping[str, Any]) -> float:
    try:
        as_of = datetime.fromisoformat(str(payload.get("as_of") or "").replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (datetime.now(UTC) - as_of).total_seconds()


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
