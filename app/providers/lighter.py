from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import httpx

from app.models import AssetConfig, Bar, Quote
from app.providers.base import QuoteProvider

BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"

# One orderBookDetails call returns quotes + metadata for every market, so the
# details cache doubles as the symbol -> market_id map. Funding rates change
# hourly; refresh them far less often than quotes.
DETAILS_TTL_SECONDS = 8.0
FUNDING_TTL_SECONDS = 300.0
RATE_LIMIT_COOLDOWN_SECONDS = 60.0

# Lighter caps candles at 500 per call.
MAX_CANDLES = 500

_RESOLUTIONS = {"1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"}


class LighterProvider(QuoteProvider):
    name = "lighter"

    def __init__(self) -> None:
        self._details: dict[str, dict[str, Any]] = {}
        self._details_time = 0.0
        self._funding: dict[str, float] = {}
        self._funding_time = 0.0
        self._cooldown_until = 0.0
        self._details_lock = asyncio.Lock()

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not assets:
            return []
        details = await self._get_details()
        if not details:
            return []
        wants_funding = any(asset.type == "crypto_perp" for asset in assets)
        funding = await self._get_funding() if wants_funding else {}
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

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        market_id = await self.market_id(asset.symbol)
        if market_id is None:
            return []
        start, end = _range_to_window(range_)
        params = {
            "market_id": market_id,
            "resolution": _normalize_interval(interval),
            "start_timestamp": int(start.timestamp() * 1000),
            "end_timestamp": int(end.timestamp() * 1000),
            "count_back": MAX_CANDLES,
        }
        payload = await self._get_json("/candles", params)
        candles = payload.get("c") if isinstance(payload, dict) else None
        if not isinstance(candles, list):
            return []
        bars: list[Bar] = []
        for raw in candles:
            bar = _bar_from_candle(asset, raw, interval)
            if bar is not None:
                bars.append(bar)
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
        """Last trade prices for the requested symbols that Lighter lists."""
        details = await self._get_details()
        prices: dict[str, float] = {}
        for symbol in symbols:
            detail = details.get(symbol.upper())
            if detail is None:
                continue
            last = _number(detail.get("last_trade_price"))
            if last is not None and last > 0:
                prices[symbol.upper()] = last
        return prices

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

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        if monotonic() < self._cooldown_until:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{BASE_URL}{path}", params=params)
                if response.status_code == 429:
                    # Standard accounts get 60 req/min per IP; back off so
                    # cached quotes serve until the window resets.
                    self._cooldown_until = monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                    return None
                response.raise_for_status()
                return response.json()
        except Exception:
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
        open_interest_usd=(
            open_interest * last if is_perp and open_interest is not None else None
        ),
    )


def _bar_from_candle(asset: AssetConfig, raw: Any, interval: str) -> Bar | None:
    if not isinstance(raw, dict):
        return None
    timestamp_ms = _number(raw.get("t"))
    open_ = _number(raw.get("o"))
    high = _number(raw.get("h"))
    low = _number(raw.get("l"))
    close = _number(raw.get("c"))
    if None in (timestamp_ms, open_, high, low, close):
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
    # 1wk falls back to daily candles: Lighter's history is shallower than
    # 500 daily bars anyway, so weekly aggregation would waste resolution.
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
