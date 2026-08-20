from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

import httpx

from app.models import AssetConfig, Bar, Quote, is_valid_bar
from app.providers.base import QuoteProvider, ValidationStatus

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"

# One metaAndAssetCtxs call per dex returns quotes, funding, OI, and volume
# for every market, so the market map doubles as the quote cache. The main
# dex carries the crypto perps; the HIP-3 "xyz" dex carries the TradFi
# synthetics (equities, ETFs, FX, commodities) that drive the 24/7 overlay.
TRADFI_DEX = "xyz"
MARKETS_TTL_SECONDS = 8.0
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
FAILURE_COOLDOWN_SECONDS = 30.0
# A market map that has not refreshed for this long must not price anything:
# the overlay would rewrite live venue quotes with dead marks and the tape
# would show frozen prices as if they were live. Stale maps still serve
# has_market/is_crypto_market lookups — routing can resolve from old listings.
MAX_QUOTE_AGE_SECONDS = 120.0

# candleSnapshot serves at most ~5000 recent candles per call — enough for
# every range the board requests at each interval, so no paging.
_INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "12h": "12h",
    "1d": "1d",
    "1wk": "1w",
    "1mo": "1M",
}


class HyperliquidProvider(QuoteProvider):
    name = "hyperliquid"

    def __init__(self) -> None:
        # Keyed by UPPER symbol; values keep the exact API coin name
        # ("kPEPE", "xyz:AAPL") for candle requests. Crypto and TradFi maps
        # stay separate: ticker collisions between a token and an equity
        # must never leak a token price into the equity overlay.
        self._crypto: dict[str, dict[str, Any]] = {}
        self._tradfi: dict[str, dict[str, Any]] = {}
        self._markets_time = 0.0
        # Per-dex parse-success stamps: one dex can fail while the other keeps
        # refreshing, and freshness decisions must track the serving map.
        self._crypto_time = 0.0
        self._tradfi_time = 0.0
        self._outage_warned_until = 0.0
        self._cooldown_until: dict[str, float] = {}
        self._markets_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not assets:
            return []
        await self._refresh_markets()
        if not self._map_fresh(self._crypto_time) and not self._map_fresh(self._tradfi_time):
            self._warn_outage_once()
        now = datetime.now(UTC)
        quotes: list[Quote] = []
        for asset in assets:
            market = self._market_for(asset.symbol, asset.type)
            if market is None:
                continue
            quote = _quote_from_market(asset, market, now)
            if quote is None:
                continue
            serving_time = (
                self._crypto_time
                if asset.type in {"crypto_perp", "crypto_spot"}
                else self._tradfi_time
            )
            if not self._map_fresh(serving_time):
                quote = replace(quote, is_stale=True)
            quotes.append(quote)
        return quotes

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        await self._refresh_markets()
        market = self._market_for(asset.symbol, asset.type)
        if market is None:
            return []
        start, end = _range_to_window(range_)
        payload = await self._post_info(
            # Per-coin cooldown key: one symbol's failing candle request must
            # never black out every other symbol's chart.
            f"candleSnapshot:{market['coin']}",
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": market["coin"],
                    "interval": _INTERVALS.get(interval, "1d"),
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(end.timestamp() * 1000),
                },
            },
        )
        if not isinstance(payload, list):
            return []
        bars = []
        for raw in payload:
            bar = _bar_from_candle(asset, raw, interval)
            if bar is not None:
                bars.append(bar)
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    async def validate_asset(self, asset: AssetConfig) -> ValidationStatus:
        await self._refresh_markets()
        if not self._crypto and not self._tradfi:
            return "unavailable"
        return "valid" if self._market_for(asset.symbol, asset.type) is not None else "not_found"

    async def has_market(self, symbol: str) -> bool:
        """Whether Hyperliquid lists a market for this symbol (cached)."""
        await self._refresh_markets()
        wanted = symbol.upper()
        return wanted in self._crypto or wanted in self._tradfi

    async def discovery_universe(self) -> dict[str, dict[str, str]]:
        """Fresh complete universes for persistent new-listing detection.

        A missing key means that dex is stale or unavailable; callers must
        leave its persisted state untouched rather than treating an outage as
        a mass delisting.
        """
        await self._refresh_markets()
        result: dict[str, dict[str, str]] = {}
        if self._map_fresh(self._crypto_time):
            result["crypto"] = {
                symbol: str(market["coin"]) for symbol, market in self._crypto.items()
            }
        if self._map_fresh(self._tradfi_time):
            result["xyz"] = {symbol: str(market["coin"]) for symbol, market in self._tradfi.items()}
        return result

    async def live_prices(self, symbols: set[str]) -> dict[str, float]:
        """Mark prices for symbols the xyz dex lists as TradFi synthetics.

        Used by the equity price overlay, so crypto markets are excluded:
        a token ticker must never price the same-named equity.
        """
        await self._refresh_markets()
        # A stale xyz map must never feed the overlay: it would replace live
        # venue quotes with frozen marks that still look authoritative.
        if not self._map_fresh(self._tradfi_time):
            return {}
        prices: dict[str, float] = {}
        for symbol in symbols:
            market = self._tradfi.get(symbol.upper())
            if market is None:
                continue
            last = market.get("last")
            if isinstance(last, float) and last > 0:
                prices[symbol.upper()] = last
        return prices

    def is_crypto_market(self, symbol: str) -> bool:
        """Whether the cached market map classifies `symbol` as a crypto perp."""
        return symbol.upper() in self._crypto

    def is_tradfi_market(self, symbol: str) -> bool:
        """Whether the cached map lists `symbol` as an xyz TradFi synthetic."""
        return symbol.upper() in self._tradfi

    def crypto_tape_cached(self) -> list[dict[str, object]]:
        """Every live crypto perp on Hyperliquid as a quote-tape row.

        Synchronous by design: the board payload builders run after the
        quote poll has refreshed the market map, so no HTTP happens here.
        Cold caches yield an empty tape until the first poll lands.
        """
        # A frozen tape is worse than an empty one: rows carry no timestamps,
        # so stale marks would read as live prices.
        if not self._map_fresh(self._crypto_time):
            return []
        tape: list[dict[str, object]] = []
        for market in self._crypto.values():
            last = market.get("last")
            if not isinstance(last, float) or last <= 0:
                continue
            display = str(market["display"])
            tape.append(
                {
                    "symbol": display,
                    "basket": _basket(display),
                    "last": last,
                    "change_pct": market.get("change_pct"),
                    "funding_rate": market.get("funding"),
                    "open_interest_usd": market.get("open_interest_usd"),
                    "day_volume_usd": market.get("day_volume_usd"),
                }
            )
        tape.sort(
            key=lambda row: cast("float | None", row.get("day_volume_usd")) or 0.0,
            reverse=True,
        )
        return tape

    def status(self) -> dict[str, object]:
        """Cache freshness and cooldowns, for the diagnostics endpoint."""
        now = monotonic()

        def age(stamp: float) -> float | None:
            return round(now - stamp, 1) if stamp > 0 else None

        return {
            "crypto_markets": len(self._crypto),
            "tradfi_markets": len(self._tradfi),
            "crypto_age_seconds": age(self._crypto_time),
            "tradfi_age_seconds": age(self._tradfi_time),
            "cooldowns_seconds": {
                key: round(until - now, 1)
                for key, until in self._cooldown_until.items()
                if until > now
            },
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _market_for(self, symbol: str, asset_type: str) -> dict[str, Any] | None:
        wanted = symbol.upper()
        if asset_type in {"crypto_perp", "crypto_spot"}:
            return self._crypto.get(wanted)
        return self._tradfi.get(wanted) or None

    async def _refresh_markets(self) -> None:
        if self._markets_time > 0 and monotonic() - self._markets_time < MARKETS_TTL_SECONDS:
            return
        async with self._markets_lock:
            if self._markets_time > 0 and monotonic() - self._markets_time < MARKETS_TTL_SECONDS:
                return
            main, tradfi = await asyncio.gather(
                self._post_info("meta", {"type": "metaAndAssetCtxs"}),
                self._post_info("meta-xyz", {"type": "metaAndAssetCtxs", "dex": TRADFI_DEX}),
            )
            crypto = _parse_universe(main, strip_prefix=None)
            synths = _parse_universe(tradfi, strip_prefix=f"{TRADFI_DEX}:")
            now = monotonic()
            if crypto:
                self._crypto = crypto
                self._crypto_time = now
            if synths:
                self._tradfi = synths
                self._tradfi_time = now
            if crypto or synths:
                self._markets_time = now

    def _map_fresh(self, stamp: float) -> bool:
        return stamp > 0 and monotonic() - stamp <= MAX_QUOTE_AGE_SECONDS

    def _warn_outage_once(self) -> None:
        """One warning per failure-cooldown window, not one per 10s poll."""
        now = monotonic()
        if now < self._outage_warned_until:
            return
        self._outage_warned_until = now + FAILURE_COOLDOWN_SECONDS
        logger.warning(
            "Hyperliquid quotes unavailable: market maps empty or older than %.0fs "
            "(crypto: %d markets, tradfi: %d markets)",
            MAX_QUOTE_AGE_SECONDS,
            len(self._crypto),
            len(self._tradfi),
        )

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _post_info(self, key: str, body: dict[str, Any]) -> Any:
        if monotonic() < self._cooldown_until.get(key, 0.0):
            return None
        try:
            response = await self._http_client().post(INFO_URL, json=body)
            if response.status_code == 429:
                self._cooldown_until[key] = monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                return None
            response.raise_for_status()
            return response.json()
        except Exception:
            self._cooldown_until[key] = monotonic() + FAILURE_COOLDOWN_SECONDS
            return None


def _parse_universe(payload: Any, *, strip_prefix: str | None) -> dict[str, dict[str, Any]]:
    """[meta, assetCtxs] -> UPPER symbol -> market record; delisted dropped."""
    if not isinstance(payload, list) or len(payload) != 2:
        return {}
    meta, ctxs = payload
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(ctxs, list):
        return {}
    markets: dict[str, dict[str, Any]] = {}
    for entry, ctx in zip(universe, ctxs, strict=False):
        if not isinstance(entry, dict) or not isinstance(ctx, dict):
            continue
        if entry.get("isDelisted"):
            continue
        coin = str(entry.get("name") or "")
        if not coin:
            continue
        display = coin
        if strip_prefix and display.startswith(strip_prefix):
            display = display[len(strip_prefix) :]
        mark = _number(ctx.get("markPx"))
        if mark is None or mark <= 0:
            continue
        prev_day = _number(ctx.get("prevDayPx"))
        change_pct = None
        if prev_day is not None and prev_day > 0:
            change_pct = round((mark - prev_day) / prev_day * 100, 6)
        open_interest = _number(ctx.get("openInterest"))
        markets[display.upper()] = {
            "coin": coin,
            "display": display,
            "last": mark,
            "prev_day": prev_day,
            "change_pct": change_pct,
            # Hyperliquid publishes the current hourly funding rate as a
            # fraction — the same unit the frontend annualizes (x24x365).
            "funding": _number(ctx.get("funding")),
            "open_interest_usd": (
                round(open_interest * mark, 2) if open_interest is not None else None
            ),
            "day_volume_usd": _number(ctx.get("dayNtlVlm")),
            "day_base_volume": _number(ctx.get("dayBaseVlm")),
        }
    return markets


def _quote_from_market(asset: AssetConfig, market: dict[str, Any], now: datetime) -> Quote | None:
    last = market.get("last")
    if not isinstance(last, float) or last <= 0:
        return None
    prev_day = market.get("prev_day")
    previous_close = prev_day if isinstance(prev_day, float) and prev_day > 0 else None
    is_perp = asset.type == "crypto_perp"
    return Quote.from_last_and_prev_close(
        symbol=asset.symbol,
        asset_type=asset.type,
        provider="hyperliquid",
        last=last,
        previous_close=previous_close,
        timestamp=now,
        currency="USD",
        funding_rate=market.get("funding") if is_perp else None,
        open_interest_usd=market.get("open_interest_usd") if is_perp else None,
        # Rolling 24h base volume: always fresh, unlike the cached daily bar
        # whose volume freezes between history refreshes. Feeds RVOL.
        volume=market.get("day_base_volume") if is_perp else None,
    )


def _bar_from_candle(asset: AssetConfig, raw: Any, interval: str) -> Bar | None:
    if not isinstance(raw, dict):
        return None
    timestamp_ms = _number(raw.get("t"))
    open_ = _number(raw.get("o"))
    high = _number(raw.get("h"))
    low = _number(raw.get("l"))
    close = _number(raw.get("c"))
    if timestamp_ms is None or open_ is None or high is None or low is None or close is None:
        return None
    bar = Bar(
        symbol=asset.symbol,
        provider="hyperliquid",
        interval=interval,
        timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=_number(raw.get("v")),
    )
    return bar if is_valid_bar(bar) else None


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


def _basket(symbol: str) -> str:
    """Tape basket for one crypto perp, from the static category snapshot.

    Hyperliquid has no category metadata, so the board keeps its own map
    (ported from the Lighter tokenlist snapshot — symbols overlap heavily);
    unmapped listings fall through to the wrapper heuristic, then Other.
    """
    categories = _CATEGORY_SNAPSHOT.get(symbol.upper())
    tags = set(categories or [])
    for tag, basket in _BASKET_PRIORITY:
        if tag in tags:
            return basket
    # Hyperliquid wraps 1000x meme units with a k prefix (kPEPE, kBONK).
    if symbol.startswith("k") or symbol.startswith("1000"):
        return "Memes"
    return "Other"


_BASKET_PRIORITY = (
    ("MEMES", "Memes"),
    ("AI", "AI"),
    ("LAYER_2", "L2"),
    ("LAYER_1", "L1"),
    ("DEFI", "DeFi"),
)

# Ported Lighter tokenlist snapshot (2026-07-04): symbol -> category tags.
_CATEGORY_SNAPSHOT: dict[str, list[str]] = {
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
