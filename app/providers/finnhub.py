from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import httpx

from app.models import AssetConfig, Bar, Quote
from app.providers.aggregate import aggregate_bars
from app.providers.base import QuoteProvider

MAX_QUOTE_CONCURRENCY = 6
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 3600.0


class FinnhubProvider(QuoteProvider):
    name = "finnhub"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._quote_semaphore = asyncio.Semaphore(MAX_QUOTE_CONCURRENCY)
        self._cooldown_until = 0.0

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not self.api_key or not assets:
            return []
        results = await asyncio.gather(*(self._get_quote(asset) for asset in assets))
        return [quote for quote in results if quote is not None]

    async def _get_quote(self, asset: AssetConfig) -> Quote | None:
        async with self._quote_semaphore:
            # This check belongs inside the semaphore: requests queued behind
            # the first bounded batch must observe a 429 before issuing HTTP.
            if monotonic() < self._cooldown_until:
                return None
            try:
                response = await self._http_client().get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": asset.symbol, "token": self.api_key},
                )
                if response.status_code == 429:
                    self._cooldown_until = monotonic() + _retry_after_seconds(
                        response.headers.get("Retry-After")
                    )
                    return None
                response.raise_for_status()
                payload = response.json()
            except Exception:
                return None
            last = _number(payload.get("c"))
            previous_close = _number(payload.get("pc"))
            # Finnhub answers unknown/delisted symbols with HTTP 200 and
            # c=0, pc=0 — treat that as "no quote", not a $0.00 print.
            if last is None or last <= 0:
                return None
            return Quote.from_last_and_prev_close(
                symbol=asset.symbol,
                asset_type=asset.type,
                provider="finnhub",
                last=last,
                previous_close=previous_close,
                timestamp=datetime.now(UTC),
                currency="USD",
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        if not self.api_key:
            return []
        # Finnhub has no native 4h resolution: fetch hourly and aggregate,
        # mirroring the Yahoo provider.
        fetch_interval = "1h" if interval == "4h" else interval
        start, end = _range_to_window(range_)
        params: dict[str, str | int] = {
            "symbol": asset.symbol,
            "resolution": _resolution(fetch_interval),
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
            "token": self.api_key,
        }
        response = await self._http_client().get(
            "https://finnhub.io/api/v1/stock/candle",
            params=params,
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("s") != "ok":
            return []
        bars: list[Bar] = []
        for timestamp, open_, high, low, close, volume in zip(
            payload.get("t", []),
            payload.get("o", []),
            payload.get("h", []),
            payload.get("l", []),
            payload.get("c", []),
            payload.get("v", []),
            strict=False,
        ):
            open_value, high_value, low_value, close_value = (
                _number(value) for value in (open_, high, low, close)
            )
            if (
                open_value is None
                or high_value is None
                or low_value is None
                or close_value is None
            ):
                continue
            # t can carry nulls too; a raw fromtimestamp() would raise and
            # discard the whole batch, so skip the entry like OHLC above.
            epoch = _number(timestamp)
            if epoch is None:
                continue
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    provider="finnhub",
                    interval=fetch_interval,
                    timestamp=datetime.fromtimestamp(epoch, tz=UTC),
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    volume=_number(volume),
                )
            )
        if interval == "4h":
            return aggregate_bars(bars, "4h")
        return bars


def _retry_after_seconds(value: str | None) -> float:
    try:
        seconds = float(value) if value is not None else 0.0
    except ValueError:
        return RATE_LIMIT_COOLDOWN_SECONDS
    if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_RETRY_AFTER_SECONDS:
        return RATE_LIMIT_COOLDOWN_SECONDS
    return seconds


def _resolution(interval: str) -> str:
    # Every UI interval maps to a REAL Finnhub resolution; the old default
    # of "D" silently served daily candles mislabeled (and cached!) as
    # 30m/1wk/1mo.
    return {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "1d": "D",
        "1wk": "W",
        "1mo": "M",
    }.get(interval, "D")


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
