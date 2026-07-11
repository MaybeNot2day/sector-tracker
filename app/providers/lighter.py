from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

import httpx

from app.models import AssetConfig, Bar, Quote
from app.providers.aggregate import aggregate_bars
from app.providers.base import QuoteProvider

BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"

# One orderBookDetails call returns quotes + metadata for every market, so the
# details cache doubles as the symbol -> market_id map. Funding rates change
# hourly; refresh them far less often than quotes.
DETAILS_TTL_SECONDS = 8.0
FUNDING_TTL_SECONDS = 300.0
# Basket tags come from /tokenlist and change only when Lighter recategorizes
# a listing.
CATEGORY_TTL_SECONDS = 3600.0
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
# Non-429 failures (timeouts, 5xx, resets) back off too: without this a
# dead secondary endpoint added its 15s timeout to EVERY quote poll (the
# funding starvation-guard runs before prices), and TTL stamps only
# advance on success so nothing else suppressed the retry.
FAILURE_COOLDOWN_SECONDS = 30.0

# Lighter caps candles at 500 per call, so wide windows (1m/1d needs 1440,
# 1h/3mo needs 2160) must page; cap the walk so a misbehaving server can't
# spin us into the rate limit.
MAX_CANDLES = 500
MAX_HISTORY_PAGES = 8

_RESOLUTIONS = {"1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"}


class LighterProvider(QuoteProvider):
    name = "lighter"

    def __init__(self) -> None:
        self._details: dict[str, dict[str, Any]] = {}
        self._details_time = 0.0
        self._funding: dict[str, float] = {}
        self._funding_time = 0.0
        # Seeded with a baked-in snapshot so baskets survive environments
        # where /tokenlist is unreachable (e.g. rate-limited shared egress
        # IPs); a successful live fetch overrides it.
        self._categories: dict[str, list[str]] = dict(_FALLBACK_CATEGORIES)
        self._categories_time = 0.0
        # Cooldowns are per endpoint: a 429 on a secondary feed (funding,
        # tokenlist) must never blackhole the orderBookDetails quote path.
        self._cooldown_until: dict[str, float] = {}
        self._details_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not assets:
            return []
        wants_funding = any(asset.type == "crypto_perp" for asset in assets)
        if wants_funding and not self._funding:
            # Starvation guard: on shared serverless egress IPs the per-IP
            # budget can run out mid-cycle, and cold instances always spend
            # their first request on orderBookDetails — so /funding-rates
            # systematically loses. When funding has never landed, fetch it
            # first; prices tolerate one details cycle served from cache.
            await self._get_funding()
        details = await self._get_details()
        if not details:
            return []
        funding = await self._get_funding() if wants_funding else {}
        if wants_funding:
            # Keep basket tags warm for the synchronous crypto tape build.
            await self._get_categories()
        now = datetime.now(UTC)
        quotes: list[Quote] = []
        for asset in assets:
            detail = details.get(asset.symbol.upper())
            if detail is None:
                continue
            quote = _quote_from_detail(asset, detail, funding, now)
            if quote is not None:
                quotes.append(quote)
        return quotes

    def status(self) -> dict[str, object]:
        """Cache freshness and cooldowns, for the diagnostics endpoint."""
        now = monotonic()

        def age(stamp: float) -> float | None:
            return round(now - stamp, 1) if stamp > 0 else None

        return {
            "details": {"symbols": len(self._details), "age_seconds": age(self._details_time)},
            "funding": {"symbols": len(self._funding), "age_seconds": age(self._funding_time)},
            "categories": {
                "symbols": len(self._categories),
                "age_seconds": age(self._categories_time),
                "live": self._categories_time > 0,
            },
            "cooldowns_seconds": {
                path: round(until - now, 1)
                for path, until in self._cooldown_until.items()
                if until > now
            },
        }

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        market_id = await self.market_id(asset.symbol)
        if market_id is None:
            return []
        start, end = _range_to_window(range_)
        end_ms = int(end.timestamp() * 1000)
        # One call returns at most MAX_CANDLES, so page backwards: while a
        # full page's oldest candle is still newer than the window start,
        # re-request with end_timestamp just before it and merge. A failed
        # later page (cooldown/error) keeps whatever already arrived.
        bars_by_ts: dict[datetime, Bar] = {}
        for _ in range(MAX_HISTORY_PAGES):
            params = {
                "market_id": market_id,
                "resolution": _normalize_interval(interval),
                "start_timestamp": int(start.timestamp() * 1000),
                "end_timestamp": end_ms,
                "count_back": MAX_CANDLES,
            }
            payload = await self._get_json("/candles", params)
            candles = payload.get("c") if isinstance(payload, dict) else None
            if not isinstance(candles, list):
                break
            added = 0
            page_oldest: datetime | None = None
            for raw in candles:
                bar = _bar_from_candle(asset, raw, interval)
                if bar is None:
                    continue
                if page_oldest is None or bar.timestamp < page_oldest:
                    page_oldest = bar.timestamp
                if bar.timestamp not in bars_by_ts:
                    bars_by_ts[bar.timestamp] = bar
                    added += 1
            if len(candles) < MAX_CANDLES or page_oldest is None:
                break  # short/empty page: history exhausted
            if page_oldest <= start:
                break  # window covered
            if added == 0:
                # Only repeats: the server pinned the page to the window start
                # (oldest-N semantics), so paging backwards can't find more.
                break
            end_ms = int(page_oldest.timestamp() * 1000) - 1
        bars = sorted(bars_by_ts.values(), key=lambda bar: bar.timestamp)
        if interval in {"1wk", "1mo"}:
            # Lighter has no weekly/monthly resolution; aggregate the daily
            # pages (via _normalize_interval) locally.
            return aggregate_bars(bars, interval)
        return bars

    async def has_market(self, symbol: str) -> bool:
        """Whether Lighter lists a market for this symbol (cached)."""
        details = await self._get_details()
        return symbol.upper() in details

    async def market_id(self, symbol: str) -> int | None:
        details = await self._get_details()
        detail = details.get(symbol.upper())
        if detail is None:
            return None
        market_id = detail.get("market_id")
        return market_id if isinstance(market_id, int) else None

    async def live_prices(self, symbols: set[str]) -> dict[str, float]:
        """Last trade prices for symbols Lighter lists as TradFi synthetics.

        Used by the equity price overlay, so crypto-classified markets are
        excluded: Lighter tickers collide with exchange tickers (its ROBO
        token would otherwise price the ROBO robotics ETF at $0.014).
        """
        details = await self._get_details()
        prices: dict[str, float] = {}
        for symbol in symbols:
            detail = details.get(symbol.upper())
            if detail is None or _is_crypto_detail(detail):
                continue
            last = _number(detail.get("last_trade_price"))
            if last is not None and last > 0:
                prices[symbol.upper()] = last
        return prices

    def is_crypto_market(self, symbol: str) -> bool:
        """Whether the cached market map classifies `symbol` as a crypto perp."""
        detail = self._details.get(symbol.upper())
        return detail is not None and _is_crypto_detail(detail)

    def crypto_tape_cached(self) -> list[dict[str, object]]:
        """Every crypto perp on Lighter as a quote-tape row, from warm caches.

        Synchronous by design: the board payload builders run after the quote
        poll has refreshed the details/funding caches, so no HTTP happens
        here. Cold caches yield an empty tape until the first poll lands.
        """
        tape: list[dict[str, object]] = []
        for symbol, detail in self._details.items():
            if not _is_crypto_detail(detail):
                continue
            last = _number(detail.get("last_trade_price"))
            if last is None or last <= 0:
                continue
            open_interest = _number(detail.get("open_interest"))
            tape.append(
                {
                    "symbol": symbol,
                    "basket": _basket(symbol, self._categories.get(symbol)),
                    "last": last,
                    "change_pct": _number(detail.get("daily_price_change")),
                    "funding_rate": self._funding.get(symbol),
                    "open_interest_usd": (
                        round(open_interest * last, 2) if open_interest is not None else None
                    ),
                    "day_volume_usd": _number(detail.get("daily_quote_token_volume")),
                }
            )
        tape.sort(
            key=lambda row: cast(float | None, row.get("day_volume_usd")) or 0.0,
            reverse=True,
        )
        return tape

    async def _get_details(self) -> dict[str, dict[str, Any]]:
        if monotonic() - self._details_time < DETAILS_TTL_SECONDS:
            return self._details
        async with self._details_lock:
            if monotonic() - self._details_time < DETAILS_TTL_SECONDS:
                return self._details
            payload = await self._get_json("/orderBookDetails", {})
            parsed = _parse_details(payload)
            if parsed:
                self._details = parsed
                self._details_time = monotonic()
            return self._details

    async def _get_funding(self) -> dict[str, float]:
        if monotonic() - self._funding_time < FUNDING_TTL_SECONDS:
            return self._funding
        payload = await self._get_json("/funding-rates", {})
        parsed = _parse_funding(payload)
        if parsed:
            self._funding = parsed
            self._funding_time = monotonic()
        return self._funding

    async def _get_categories(self) -> dict[str, list[str]]:
        if monotonic() - self._categories_time < CATEGORY_TTL_SECONDS:
            return self._categories
        payload = await self._get_json("/tokenlist", {})
        parsed = _parse_categories(payload)
        if parsed:
            self._categories = parsed
            self._categories_time = monotonic()
        return self._categories

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        if monotonic() < self._cooldown_until.get(path, 0.0):
            return None
        try:
            response = await self._http_client().get(f"{BASE_URL}{path}", params=params)
            if response.status_code == 429:
                # Standard accounts get 60 req/min per IP; back off this
                # endpoint so cached data serves until the window resets.
                self._cooldown_until[path] = monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                return None
            response.raise_for_status()
            return response.json()
        except Exception:
            self._cooldown_until[path] = monotonic() + FAILURE_COOLDOWN_SECONDS
            return None


def _parse_details(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    details = payload.get("order_book_details")
    if not isinstance(details, list):
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for detail in details:
        if not isinstance(detail, dict):
            continue
        symbol = str(detail.get("symbol", "")).upper()
        if symbol and detail.get("status") == "active":
            parsed[symbol] = detail
    return parsed


# Lighter has no explicit asset-class field; strategy_index clusters markets
# (verified against all 214 live markets): 2 = crypto perps, 3 = commodities,
# 4 = FX, 5 = US equities/ETFs, 6 = Asia equities, 7 = pre-IPO synthetics.
# 0 is a legacy bucket mixing early meme coins with a handful of TradFi
# listings, disambiguated by the exclusion set below.
_TRADFI_STRATEGY_INDEXES = {3, 4, 5, 6, 7}
_LEGACY_TRADFI_SYMBOLS = {
    "DIA",
    "HANMI",
    "HYUNDAI",
    "KRCOMP",
    "MAGS",
    "SAMSUNG",
    "SKHYNIX",
    "SOXX",
    "SPACEX",
}


def _is_crypto_detail(detail: dict[str, Any]) -> bool:
    strategy = detail.get("strategy_index")
    if isinstance(strategy, int) and strategy in _TRADFI_STRATEGY_INDEXES:
        return False
    symbol = str(detail.get("symbol", "")).upper()
    return symbol not in _LEGACY_TRADFI_SYMBOLS


def _parse_funding(payload: Any) -> dict[str, float]:
    """Lighter's own funding rate per symbol, converted to an hourly fraction.

    The endpoint returns 8h-normalized rates across venues (verified: the
    hyperliquid entry equals HL's native hourly rate x 8), so divide by 8 to
    keep the Quote contract of an hourly funding fraction.
    """
    if not isinstance(payload, dict):
        return {}
    rates = payload.get("funding_rates")
    if not isinstance(rates, list):
        return {}
    parsed: dict[str, float] = {}
    for entry in rates:
        if not isinstance(entry, dict) or entry.get("exchange") != "lighter":
            continue
        symbol = str(entry.get("symbol", "")).upper()
        rate = _number(entry.get("rate"))
        if symbol and rate is not None:
            parsed[symbol] = rate / 8.0
    return parsed


def _parse_categories(payload: Any) -> dict[str, list[str]]:
    """Lighter's own basket tags per crypto token from /tokenlist."""
    if not isinstance(payload, dict):
        return {}
    tokens = payload.get("tokens")
    if not isinstance(tokens, list):
        return {}
    parsed: dict[str, list[str]] = {}
    for token in tokens:
        if not isinstance(token, dict) or token.get("asset_type") != "CRYPTO":
            continue
        symbol = str(token.get("symbol", "")).upper()
        categories = token.get("categories")
        if symbol and isinstance(categories, list):
            parsed[symbol] = [str(category).upper() for category in categories]
    return parsed


# Mirror Lighter's app baskets. Multi-tagged tokens (e.g. CHIP = DEFI + AI)
# land in the most specific basket, so the priority runs narrow to broad.
_BASKET_PRIORITY = (
    ("MEMES", "Memes"),
    ("AI", "AI"),
    ("LAYER_2", "L2"),
    ("LAYER_1", "L1"),
    ("DEFI", "DeFi"),
)


def _basket(symbol: str, categories: list[str] | None) -> str:
    tags = set(categories or [])
    for tag, basket in _BASKET_PRIORITY:
        if tag in tags:
            return basket
    # Lighter leaves its 1000x-wrapped meme listings untagged.
    if symbol.startswith("1000"):
        return "Memes"
    return "Other"


def _quote_from_detail(
    asset: AssetConfig,
    detail: dict[str, Any],
    funding: dict[str, float],
    now: datetime,
) -> Quote | None:
    last = _number(detail.get("last_trade_price"))
    if last is None or last <= 0:
        return None
    change_pct = _number(detail.get("daily_price_change"))
    previous_close = None
    if change_pct is not None and change_pct > -100:
        previous_close = last / (1 + change_pct / 100)
    open_interest = _number(detail.get("open_interest"))
    is_perp = asset.type == "crypto_perp"
    return Quote.from_last_and_prev_close(
        symbol=asset.symbol,
        asset_type=asset.type,
        provider="lighter",
        last=last,
        previous_close=previous_close,
        timestamp=now,
        currency="USD",
        funding_rate=funding.get(asset.symbol.upper()) if is_perp else None,
        open_interest_usd=(open_interest * last if is_perp and open_interest is not None else None),
        # Rolling 24h base-token volume: always fresh, unlike the cached
        # daily bar whose volume freezes between history refreshes (there is
        # no background refresh at all on serverless). Feeds RVOL.
        volume=_number(detail.get("daily_base_token_volume")) if is_perp else None,
    )


def _bar_from_candle(asset: AssetConfig, raw: Any, interval: str) -> Bar | None:
    if not isinstance(raw, dict):
        return None
    timestamp_ms = _number(raw.get("t"))
    open_ = _number(raw.get("o"))
    high = _number(raw.get("h"))
    low = _number(raw.get("l"))
    close = _number(raw.get("c"))
    if (
        timestamp_ms is None
        or open_ is None
        or high is None
        or low is None
        or close is None
    ):
        return None
    return Bar(
        symbol=asset.symbol,
        provider="lighter",
        interval=interval,
        timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=_number(raw.get("v")),
    )


def _normalize_interval(interval: str) -> str:
    if interval in _RESOLUTIONS:
        return interval
    # 1wk/1mo fetch daily candles and aggregate locally in get_history.
    return "1d"


def _range_to_window(range_: str) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    today = end.date()
    start = {
        "10m": end - timedelta(minutes=10),
        "30m": end - timedelta(minutes=30),
        "1h": end - timedelta(hours=1),
        "4h": end - timedelta(hours=4),
        "1d": end - timedelta(days=1),
        "1w": end - timedelta(days=7),
        "1mo": end - timedelta(days=31),
        "3mo": end - timedelta(days=93),
        "6mo": end - timedelta(days=186),
        "1y": end - timedelta(days=366),
        "5y": end - timedelta(days=366 * 5),
        "10y": end - timedelta(days=366 * 10),
        "ytd": datetime(today.year, 1, 1, tzinfo=UTC),
    }.get(range_, end - timedelta(days=366))
    return start, end


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


# Snapshot of Lighter /tokenlist categories (2026-07-04). Used only until
# the live tokenlist fetch succeeds; keeps baskets working when the endpoint
# is unreachable. Symbols absent here fall through to _basket heuristics.
_FALLBACK_CATEGORIES: dict[str, list[str]] = {
    "0G": ["LAYER_1"],
    "AAVE": ["DEFI"],
    "ADA": ["LAYER_1"],
    "ADI": ["NEW", "LAYER_2"],
    "AERO": ["DEFI"],
    "AI16Z": ["MEMES"],
    "APEX": ["DEFI"],
    "APT": ["DEFI"],
    "ARB": ["LAYER_2"],
    "ARC": ["AI"],
    "ASTER": ["DEFI"],
    "AVAX": ["LAYER_1"],
    "AVNT": ["DEFI"],
    "AZTEC": ["LAYER_2"],
    "BCH": ["LAYER_1"],
    "BERA": ["LAYER_1"],
    "BIRB": ["MEMES"],
    "BNB": ["LAYER_1"],
    "BTC": ["MAJOR", "LAYER_1"],
    "CAP": ["NEW", "DEFI"],
    "CC": ["LAYER_1"],
    "CHIP": ["AI", "DEFI"],
    "CRO": ["LAYER_1"],
    "CRV": ["DEFI"],
    "CTR": ["LAYER_2"],
    "DATA": ["LAYER_1"],
    "DOGE": ["MEMES"],
    "DOLO": ["DEFI"],
    "DOT": ["LAYER_1"],
    "DUSK": ["LAYER_1"],
    "DYDX": ["DEFI"],
    "EDEN": ["DEFI"],
    "EIGEN": ["DEFI"],
    "ENA": ["DEFI"],
    "ETH": ["MAJOR", "LAYER_1"],
    "ETHFI": ["DEFI"],
    "FARTCOIN": ["MEMES"],
    "FF": ["DEFI"],
    "FIL": ["LAYER_1", "AI"],
    "FOGO": ["DEFI", "LAYER_1"],
    "GMX": ["DEFI"],
    "GRAM": ["LAYER_1"],
    "GRASS": ["AI"],
    "HBAR": ["LAYER_1"],
    "HYPE": ["DEFI", "LAYER_1"],
    "ICP": ["LAYER_1", "AI"],
    "IP": ["LAYER_1"],
    "JTO": ["DEFI"],
    "JUP": ["DEFI"],
    "KAITO": ["DEFI", "AI"],
    "KBONK": ["MEMES"],
    "KFLOKI": ["MEMES"],
    "KNOT": ["MEMES"],
    "KPEPE": ["MEMES"],
    "KSHIB": ["MEMES"],
    "KTOSHI": ["MEMES"],
    "LAUNCHCOIN": ["MEMES"],
    "LDO": ["DEFI"],
    "LINEA": ["LAYER_2"],
    "LINK": ["DEFI"],
    "LIT": ["DEFI", "LAYER_2"],
    "LTC": ["LAYER_1"],
    "MEGA": ["LAYER_2"],
    "MET": ["DEFI"],
    "MKR": ["DEFI"],
    "MNT": ["LAYER_2"],
    "MON": ["LAYER_1"],
    "MORPHO": ["DEFI"],
    "MYX": ["DEFI"],
    "NEAR": ["LAYER_1", "AI"],
    "NMR": ["DEFI", "AI"],
    "ONDO": ["DEFI"],
    "OP": ["LAYER_2"],
    "PENDLE": ["DEFI"],
    "PENGU": ["MEMES"],
    "PIPPIN": ["MEMES"],
    "POL": ["LAYER_2"],
    "POPCAT": ["MEMES"],
    "PUMP": ["MEMES"],
    "PYTH": ["DEFI"],
    "RESOLV": ["DEFI"],
    "ROBO": ["AI"],
    "S": ["LAYER_1"],
    "SEI": ["DEFI"],
    "SKY": ["DEFI"],
    "SOL": ["MAJOR", "LAYER_1"],
    "SPX": ["MEMES"],
    "STABLE": ["LAYER_1"],
    "STBL": ["DEFI"],
    "STRK": ["LAYER_2"],
    "SUI": ["LAYER_1"],
    "SYRUP": ["DEFI"],
    "TAO": ["AI"],
    "TIA": ["LAYER_1"],
    "TON": ["LAYER_1"],
    "TRUMP": ["MEMES"],
    "TRX": ["LAYER_1"],
    "UNI": ["DEFI"],
    "USDHKD": ["NEW"],
    "USELESS": ["MEMES"],
    "VIRTUAL": ["DEFI", "AI"],
    "VVV": ["DEFI", "AI"],
    "WIF": ["MEMES"],
    "WLD": ["AI"],
    "XLM": ["LAYER_1"],
    "XMR": ["LAYER_1"],
    "XPL": ["LAYER_1"],
    "YZY": ["MEMES"],
    "ZEC": ["LAYER_1"],
    "ZK": ["LAYER_2"],
}
