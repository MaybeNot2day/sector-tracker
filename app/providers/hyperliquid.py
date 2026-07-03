from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.models import AssetConfig, Bar, Quote
from app.providers.base import QuoteProvider


class HyperliquidProvider(QuoteProvider):
    name = "hyperliquid"
    base_url = "https://api.hyperliquid.xyz/info"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not assets:
            return []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.base_url, json={"type": "metaAndAssetCtxs"})
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return []

        markets = _parse_market_contexts(payload)
        now = datetime.now(UTC)
        quotes: list[Quote] = []
        for asset in assets:
            market = markets.get(asset.symbol.upper())
            if market is None:
                continue
            mark = _number(market.get("markPx"))
            last = _number(market.get("midPx")) or mark
            previous_close = _number(market.get("prevDayPx"))
            if last is None:
                continue
            open_interest = _number(market.get("openInterest"))
            quotes.append(
                Quote.from_last_and_prev_close(
                    symbol=asset.symbol,
                    asset_type=asset.type,
                    provider="hyperliquid",
                    last=last,
                    previous_close=previous_close,
                    timestamp=now,
                    currency="USD",
                    funding_rate=_number(market.get("funding")),
                    open_interest_usd=(
                        open_interest * mark
                        if open_interest is not None and mark is not None
                        else None
                    ),
                )
            )
        return quotes

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        start, end = _range_to_window(range_)
        request = {
            "type": "candleSnapshot",
            "req": {
                "coin": asset.symbol.upper(),
                "interval": _normalize_interval(interval),
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(self.base_url, json=request)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            return []
        bars: list[Bar] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            timestamp_ms = _number(raw.get("t"))
            open_ = _number(raw.get("o"))
            high = _number(raw.get("h"))
            low = _number(raw.get("l"))
            close = _number(raw.get("c"))
            if None in (timestamp_ms, open_, high, low, close):
                continue
            bars.append(
                Bar(
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
            )
        return bars


def _parse_market_contexts(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) != 2:
        return {}
    meta, contexts = payload
    if not isinstance(meta, dict) or not isinstance(contexts, list):
        return {}
    universe = meta.get("universe", [])
    if not isinstance(universe, list):
        return {}

    markets: dict[str, dict[str, Any]] = {}
    for coin, context in zip(universe, contexts, strict=False):
        if not isinstance(coin, dict) or not isinstance(context, dict):
            continue
        name = str(coin.get("name", "")).upper()
        if name:
            markets[name] = context
    return markets


def _normalize_interval(interval: str) -> str:
    return {
        "1d": "1d",
        "1h": "1h",
        "4h": "4h",
        "15m": "15m",
        "5m": "5m",
        "1m": "1m",
    }.get(interval, "1d")


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
