from __future__ import annotations

import csv
import math
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.models import AssetConfig, Bar, Quote
from app.providers.base import QuoteProvider


class StooqProvider(QuoteProvider):
    name = "stooq"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        if not assets:
            return []
        symbols = ",".join(_stooq_symbol(asset.symbol) for asset in assets)
        url = "https://stooq.com/q/l/"
        params = {"s": symbols, "f": "sd2t2ohlcv", "h": "", "e": "csv"}
        try:
            response = await self._http_client().get(url, params=params)
            response.raise_for_status()
        except Exception:
            return []

        by_stooq_symbol = {_stooq_symbol(asset.symbol).upper(): asset for asset in assets}
        quotes: list[Quote] = []
        for row in csv.DictReader(StringIO(response.text)):
            asset = by_stooq_symbol.get(str(row.get("Symbol", "")).upper())
            close = _number(row.get("Close"))
            timestamp = _parse_stooq_datetime(row.get("Date"), row.get("Time"))
            # Suspended/unknown symbols come back as N/D; stamping them "now"
            # would make the freshness heuristic treat dead quotes as live.
            if asset is None or close is None or timestamp is None:
                continue
            quotes.append(
                Quote.from_last_and_prev_close(
                    symbol=asset.symbol,
                    asset_type=asset.type,
                    provider="stooq",
                    last=close,
                    previous_close=None,
                    timestamp=timestamp,
                    currency=_stooq_currency(asset.symbol),
                )
            )
        return quotes

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        if interval != "1d":
            return []
        url = "https://stooq.com/q/d/l/"
        params = {"s": _stooq_symbol(asset.symbol), "i": "d", **_history_date_params(range_)}
        response = await self._http_client().get(url, params=params, timeout=15.0)
        response.raise_for_status()
        bars: list[Bar] = []
        for row in csv.DictReader(StringIO(response.text)):
            try:
                timestamp = datetime.strptime(str(row["Date"]), "%Y-%m-%d").replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            open_ = _number(row.get("Open"))
            high = _number(row.get("High"))
            low = _number(row.get("Low"))
            close = _number(row.get("Close"))
            if open_ is None or high is None or low is None or close is None:
                continue
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    provider="stooq",
                    interval=interval,
                    timestamp=timestamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=_number(row.get("Volume")),
                )
            )
        return _range_filter(bars, range_)


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _history_date_params(range_: str) -> dict[str, str]:
    today = _utc_today()
    params = {"d2": today.strftime("%Y%m%d")}
    day_counts = {
        "1d": 1,
        "1w": 7,
        "1mo": 31,
        "3mo": 93,
        "6mo": 186,
        "1y": 366,
        "5y": 366 * 5,
    }
    if range_ in day_counts:
        overfetch_days = 14 if range_ == "1d" else 7
        start = today - timedelta(days=day_counts[range_] + overfetch_days)
    elif range_ == "ytd":
        start = date(today.year, 1, 1) - timedelta(days=7)
    else:
        return params
    params["d1"] = start.strftime("%Y%m%d")
    return params


def _stooq_symbol(symbol: str) -> str:
    lowered = symbol.lower()
    if "." in lowered:
        return lowered
    return f"{lowered}.us"


def _stooq_currency(symbol: str) -> str | None:
    lowered = symbol.lower()
    if "." not in lowered or lowered.endswith(".us"):
        return "USD"
    return None


# Stooq is a Polish service; its quote CSV t2 field is Warsaw wall-clock
# time, not UTC. Parsing it as UTC put timestamps up to 2h in the FUTURE,
# which made the Lighter overlay's freshness heuristic treat stale quotes
# as fresh. If the feed is ever exchange-local instead, Warsaw errs on the
# stale side — the safe direction for that heuristic.
_STOOQ_ZONE = ZoneInfo("Europe/Warsaw")


def _parse_stooq_datetime(date_value: Any, time_value: Any) -> datetime | None:
    date_text = str(date_value or "")
    time_text = str(time_value or "00:00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(f"{date_text} {time_text}", fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=_STOOQ_ZONE).astimezone(UTC)
    return None


def _range_filter(bars: list[Bar], range_: str) -> list[Bar]:
    if not bars:
        return bars
    end = bars[-1].timestamp
    day_counts = {
        "1d": 1,
        "1w": 7,
        "1mo": 31,
        "3mo": 93,
        "6mo": 186,
        "1y": 366,
        "5y": 366 * 5,
    }
    if range_ in day_counts:
        start = end - timedelta(days=day_counts[range_])
    elif range_ == "ytd":
        start = datetime(end.year, 1, 1, tzinfo=UTC)
    else:
        start = None
    if start is None:
        return bars
    return [bar for bar in bars if bar.timestamp >= start]


def _number(value: Any) -> float | None:
    if value in (None, "", "N/D"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed
