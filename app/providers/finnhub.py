from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.models import AssetConfig, Bar, Quote
from app.providers.base import QuoteProvider


class FinnhubProvider(QuoteProvider):
    name = "finnhub"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not self.api_key:
            return []
        quotes: list[Quote] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for asset in assets:
                try:
                    response = await client.get(
                        "https://finnhub.io/api/v1/quote",
                        params={"symbol": asset.symbol, "token": self.api_key},
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception:
                    continue
                last = _number(payload.get("c"))
                previous_close = _number(payload.get("pc"))
                if last is None:
                    continue
                quotes.append(
                    Quote.from_last_and_prev_close(
                        symbol=asset.symbol,
                        asset_type=asset.type,
                        provider="finnhub",
                        last=last,
                        previous_close=previous_close,
                        timestamp=datetime.now(UTC),
                    )
                )
        return quotes

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        if not self.api_key:
            return []
        start, end = _range_to_window(range_)
        params = {
            "symbol": asset.symbol,
            "resolution": _resolution(interval),
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
            "token": self.api_key,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get("https://finnhub.io/api/v1/stock/candle", params=params)
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
            parsed = [_number(v) for v in (open_, high, low, close)]
            if None in parsed:
                continue
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    provider="finnhub",
                    interval=interval,
                    timestamp=datetime.fromtimestamp(float(timestamp), tz=UTC),
                    open=parsed[0],
                    high=parsed[1],
                    low=parsed[2],
                    close=parsed[3],
                    volume=_number(volume),
                )
            )
        return bars


def _resolution(interval: str) -> str:
    return {"1d": "D", "1h": "60", "15m": "15", "5m": "5", "1m": "1"}.get(interval, "D")


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
