"""Same-origin proxy for asset logos.

The CSP pins img-src to 'self', so the browser can never hotlink third-party
logo CDNs directly (same reasoning as the PCPartPicker trend-image proxy).
Two upstream sources cover the board's asset classes:

- crypto perps: Hyperliquid's own coin icons (SVG), which exist for every
  market the board can quote through the Hyperliquid provider;
- equities/ETFs: Parqet's public ticker logo CDN (PNG).

Logos are immutable in practice, so hits cache for a week in memory and ship
with a long client Cache-Control. Misses are negative-cached for six hours so
unknown tickers do not hammer the upstreams on every board render.
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
# key -> (fetched_at, body or None for a negative entry, content type)
_logo_cache: dict[str, tuple[float, bytes | None, str]] = {}


async def fetch_symbol_logo(symbol: str, kind: str) -> tuple[bytes, str] | None:
    """Logo bytes and content type, or None when the upstream has no logo."""
    template = _SOURCES.get(kind)
    if template is None:
        return None
    key = f"{kind}:{symbol}"
    now = monotonic()
    cached = _logo_cache.get(key)
    if cached is not None:
        fetched_at, body, content_type = cached
        ttl = _HIT_TTL_SECONDS if body is not None else _MISS_TTL_SECONDS
        if now - fetched_at < ttl:
            return (body, content_type) if body is not None else None
    result = await _fetch_upstream(template.format(symbol=symbol))
    if result is None and cached is not None and cached[1] is not None:
        # Upstream hiccup: keep serving the stale logo rather than flashing
        # the fallback monogram across the board.
        return cached[1], cached[2]
    while len(_logo_cache) >= _CACHE_MAX:
        oldest = min(_logo_cache, key=lambda item: _logo_cache[item][0])
        del _logo_cache[oldest]
    if result is None:
        _logo_cache[key] = (now, None, "")
        return None
    _logo_cache[key] = (now, result[0], result[1])
    return result


async def _fetch_upstream(url: str) -> tuple[bytes, str] | None:
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if not content_type.startswith("image/"):
                return None
            content = response.content
            if not content or len(content) > LOGO_MAX_BYTES:
                return None
            return content, content_type
    except httpx.HTTPError:
        logger.warning("symbol logo fetch failed for %s", url)
        return None
