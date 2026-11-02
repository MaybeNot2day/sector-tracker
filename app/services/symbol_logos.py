"""Same-origin proxy for asset logos.

The CSP pins img-src to 'self', so the browser can never hotlink third-party
logo CDNs directly (same reasoning as the PCPartPicker trend-image proxy).
Two upstream sources cover the board's asset classes:

- crypto perps: Hyperliquid's own coin icons (SVG), which exist for every
  market the board can quote through the Hyperliquid provider;
- equities/ETFs: Parqet's public ticker logo CDN (PNG).

Logos are immutable in practice, so hits cache for a week in memory and ship
with a long client Cache-Control. Definitive misses (404/410, non-image) are
negative-cached for six hours so unknown tickers do not hammer the upstreams
on every board render; transient failures (429, 5xx, network) only briefly.
"""

from __future__ import annotations

import logging
from time import monotonic

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 8.0
LOGO_MAX_BYTES = 512 * 1024
LOGO_KINDS = ("crypto", "stock")

_SOURCES: dict[str, str] = {
    "crypto": "https://app.hyperliquid.xyz/coins/{symbol}.svg",
    "stock": "https://assets.parqet.com/logos/symbol/{symbol}?format=png",
}

_CACHE_MAX = 600
_HIT_TTL_SECONDS = 7 * 24 * 3600.0
_MISS_TTL_SECONDS = 6 * 3600.0
_TRANSIENT_MISS_TTL_SECONDS = 60.0
_DEFINITIVE_MISS_STATUSES = frozenset({404, 410})
# key -> (fetched_at, body or None for a negative entry, content type, ttl)
_logo_cache: dict[str, tuple[float, bytes | None, str, float]] = {}


class _TransientLogoError(Exception):
    """Upstream failure that says nothing about whether the logo exists."""


async def fetch_symbol_logo(symbol: str, kind: str) -> tuple[bytes, str] | None:
    """Logo bytes and content type, or None when the upstream has no logo."""
    template = _SOURCES.get(kind)
    if template is None:
        return None
    key = f"{kind}:{symbol}"
    now = monotonic()
    cached = _logo_cache.get(key)
    if cached is not None:
        fetched_at, body, content_type, ttl = cached
        if now - fetched_at < ttl:
            return (body, content_type) if body is not None else None
    try:
        result = await _fetch_upstream(template.format(symbol=symbol))
        miss_ttl = _MISS_TTL_SECONDS
    except _TransientLogoError:
        result = None
        miss_ttl = _TRANSIENT_MISS_TTL_SECONDS
    if result is None and cached is not None and cached[1] is not None:
        # Upstream hiccup: keep serving the stale logo rather than flashing
        # the fallback monogram across the board.
        return cached[1], cached[2]
    while len(_logo_cache) >= _CACHE_MAX:
        oldest = min(_logo_cache, key=lambda item: _logo_cache[item][0])
        del _logo_cache[oldest]
    if result is None:
        _logo_cache[key] = (now, None, "", miss_ttl)
        return None
    _logo_cache[key] = (now, result[0], result[1], _HIT_TTL_SECONDS)
    return result


async def _fetch_upstream(url: str) -> tuple[bytes, str] | None:
    """Logo, None for a definitive miss; raises _TransientLogoError otherwise."""
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("symbol logo fetch failed for %s", url)
        raise _TransientLogoError(url) from exc
    if response.status_code in _DEFINITIVE_MISS_STATUSES:
        return None
    if response.status_code != 200:
        raise _TransientLogoError(f"{url}: HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        return None
    content = response.content
    if not content or len(content) > LOGO_MAX_BYTES:
        return None
    return content, content_type
