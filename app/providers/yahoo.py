from __future__ import annotations

import asyncio
import json
import math
import subprocess
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from time import sleep
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import urlencode

import yfinance as yf

from app.models import AssetConfig, Bar, Quote
from app.providers.base import QuoteProvider

YAHOO_SPARK_URLS = (
    "https://query1.finance.yahoo.com/v7/finance/spark",
    "https://query2.finance.yahoo.com/v7/finance/spark",
)
YAHOO_CHART_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
)
YAHOO_SPARK_CHUNK_SIZE = 6
YAHOO_SPARK_CHUNK_DELAY_SECONDS = 1.0
YAHOO_MAX_CHART_FALLBACKS = 8
YAHOO_USER_AGENT = "Mozilla/5.0"


class YahooProvider(QuoteProvider):
    name = "yahoo"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return await asyncio.to_thread(self._get_quotes_sync, assets)

    def _get_quotes_sync(self, assets: list[AssetConfig]) -> list[Quote]:
        if not assets:
            return []
        unique_assets = list({asset.symbol: asset for asset in assets}.values())
        quotes_by_symbol = self._get_spark_quotes_sync(unique_assets)
        missing_assets = [
            asset for asset in unique_assets if asset.symbol not in quotes_by_symbol
        ]
        for asset in missing_assets[:YAHOO_MAX_CHART_FALLBACKS]:
            quote = self._get_chart_quote_sync(asset)
            if quote is not None:
                quotes_by_symbol[quote.symbol] = quote
        return list(quotes_by_symbol.values())

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return await asyncio.to_thread(self._get_history_sync, asset, interval, range_)

    def _get_spark_quotes_sync(
        self,
        assets: list[AssetConfig],
    ) -> dict[str, Quote]:
        quotes_by_symbol: dict[str, Quote] = {}
        for index, chunk in enumerate(_chunks(assets, YAHOO_SPARK_CHUNK_SIZE)):
            if index > 0:
                sleep(YAHOO_SPARK_CHUNK_DELAY_SECONDS)
            symbols = ",".join(asset.symbol for asset in chunk)
            asset_by_symbol = {asset.symbol.upper(): asset for asset in chunk}
            try:
                payload = _get_json_with_retry(
                    YAHOO_SPARK_URLS,
                    params={"symbols": symbols, "interval": "1m", "range": "1d"},
                )
                quotes_by_symbol.update(_quotes_from_spark_payload(asset_by_symbol, payload))
            except Exception:
                continue
        return quotes_by_symbol

    def _get_chart_quote_sync(
        self,
        asset: AssetConfig,
    ) -> Quote | None:
        try:
            payload = _get_json_with_retry(
                tuple(
                    url.format(symbol=url_quote(asset.symbol, safe=""))
                    for url in YAHOO_CHART_URLS
                ),
                params={"interval": "1m", "range": "1d"},
            )
            result = _first_chart_result(payload)
            if result is None:
                return None
            return _quote_from_chart_result(asset, result)
        except Exception:
            return None

    def _get_history_sync(self, asset: AssetConfig, interval: str, range_: str) -> list[Bar]:
        ticker = yf.Ticker(asset.symbol)
        df = ticker.history(
            period=_yahoo_period(range_),
            interval=interval,
            auto_adjust=False,
            prepost=True,
        )
        bars: list[Bar] = []
        if df.empty:
            return bars
        for idx, row in df.iterrows():
            open_ = _number(row.get("Open"))
            high = _number(row.get("High"))
            low = _number(row.get("Low"))
            close = _number(row.get("Close"))
            if open_ is None or high is None or low is None or close is None:
                continue
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    provider="yahoo",
                    interval=interval,
                    timestamp=ts,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=_number(row.get("Volume")),
                )
            )
        return bars


def _get_json_with_retry(
    urls: Sequence[str],
    *,
    params: dict[str, str],
) -> dict[str, Any]:
    error: Exception | None = None
    for attempt in range(3):
        for url in urls:
            try:
                return _get_json(url, params)
            except Exception as exc:
                error = exc
                continue
        if attempt < 2:
            sleep(1.5 * (attempt + 1))
    if error is not None:
        raise error
    raise RuntimeError("request did not run")


def _get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "curl",
            "-fsSL",
            "-A",
            YAHOO_USER_AGENT,
            "--max-time",
            "10",
            f"{url}?{urlencode(params)}",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _chunks(items: list[AssetConfig], size: int) -> Iterable[list[AssetConfig]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _quotes_from_spark_payload(
    asset_by_symbol: dict[str, AssetConfig],
    payload: dict[str, Any],
) -> dict[str, Quote]:
    spark = payload.get("spark")
    if not isinstance(spark, dict):
        return {}
    results = spark.get("result")
    if not isinstance(results, list):
        return {}

    quotes: dict[str, Quote] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).upper()
        asset = asset_by_symbol.get(symbol)
        responses = item.get("response")
        if asset is None or not isinstance(responses, list) or not responses:
            continue
        result = responses[0]
        if not isinstance(result, dict):
            continue
        quote = _quote_from_chart_result(asset, result)
        if quote is not None:
            quotes[quote.symbol] = quote
    return quotes


def _first_chart_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return None
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        return None
    result = results[0]
    return result if isinstance(result, dict) else None


def _quote_from_chart_result(asset: AssetConfig, result: dict[str, Any]) -> Quote | None:
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return None
    market_price = _latest_market_price(meta)
    last = market_price[0] if market_price else _last_chart_close(result)
    previous_close = _first_float(meta, "previousClose", "chartPreviousClose")
    if last is None:
        return None
    return Quote.from_last_and_prev_close(
        symbol=asset.symbol,
        asset_type=asset.type,
        provider="yahoo",
        last=last,
        previous_close=previous_close,
        timestamp=market_price[1] if market_price else datetime.now(UTC),
        currency=_currency(meta),
    )


def _currency(meta: dict[str, Any]) -> str | None:
    value = meta.get("currency")
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value or None


def _latest_market_price(meta: dict[str, Any]) -> tuple[float, datetime] | None:
    candidates: list[tuple[float, float]] = []
    for price_key, time_key in (
        ("regularMarketPrice", "regularMarketTime"),
        ("postMarketPrice", "postMarketTime"),
        ("preMarketPrice", "preMarketTime"),
    ):
        price = _number(meta.get(price_key))
        timestamp = _number(meta.get(time_key)) or 0.0
        if price is not None:
            candidates.append((timestamp, price))
    if not candidates:
        return None
    timestamp, price = max(candidates, key=lambda item: item[0])
    if timestamp <= 0:
        return price, datetime.now(UTC)
    return price, datetime.fromtimestamp(timestamp, UTC)


def _last_chart_close(result: dict[str, Any]) -> float | None:
    indicators = result.get("indicators")
    if not isinstance(indicators, dict):
        return None
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or not quotes:
        return None
    quote = quotes[0]
    if not isinstance(quote, dict):
        return None
    closes = quote.get("close")
    if not isinstance(closes, list):
        return None
    for close in reversed(closes):
        parsed = _number(close)
        if parsed is not None:
            return parsed
    return None


def _first_float(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in raw:
            parsed = _number(raw[key])
            if parsed is not None:
                return parsed
    return None


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


def _yahoo_period(range_: str) -> str:
    return {
        "10m": "1d",
        "30m": "1d",
        "1h": "1d",
        "4h": "1d",
        "1d": "1d",
        "1w": "5d",
        "1mo": "1mo",
        "3mo": "3mo",
        "ytd": "ytd",
    }.get(range_, range_)
