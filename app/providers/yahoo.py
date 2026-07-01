from __future__ import annotations

import asyncio
import bisect
import json
import math
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from time import sleep
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import urlencode

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
YAHOO_USD_FX_SYMBOLS = {
    "KRW": "KRW=X",
}


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
        return list(self._with_usd_display_quotes(quotes_by_symbol).values())

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

    def _with_usd_display_quotes(self, quotes_by_symbol: dict[str, Quote]) -> dict[str, Quote]:
        currencies = {
            quote.currency
            for quote in quotes_by_symbol.values()
            if quote.currency and quote.currency != "USD"
        }
        fx_assets = [
            AssetConfig(symbol=symbol, type="index_proxy", source="yahoo", name=currency)
            for currency, symbol in YAHOO_USD_FX_SYMBOLS.items()
            if currency in currencies
        ]
        if not fx_assets:
            return quotes_by_symbol
        fx_quotes = self._get_spark_quotes_sync(fx_assets)
        fx_by_currency = {
            asset.name: fx_quotes[asset.symbol]
            for asset in fx_assets
            if asset.name and asset.symbol in fx_quotes
        }
        return {
            symbol: _quote_with_usd_display(quote, fx_by_currency.get(quote.currency or ""))
            for symbol, quote in quotes_by_symbol.items()
        }

    def _get_history_sync(self, asset: AssetConfig, interval: str, range_: str) -> list[Bar]:
        bars = _get_raw_history_sync(asset, interval, range_)
        return _bars_with_usd_display(asset, bars, interval, range_)


def _get_raw_history_sync(asset: AssetConfig, interval: str, range_: str) -> list[Bar]:
    """Fetch OHLCV bars from Yahoo's v8 chart API.

    Uses the same curl transport + query1/query2 retry as quotes; yfinance's
    cookie/crumb scraping gets rate-limited from datacenter IPs (Vercel),
    which made history silently fall back to stale cached bars.
    """
    params = {
        "interval": interval,
        "range": _yahoo_period(range_),
        "includePrePost": "true",
        "events": "div,splits",
    }
    try:
        payload = _get_json_with_retry(
            tuple(
                url.format(symbol=url_quote(asset.symbol, safe=""))
                for url in YAHOO_CHART_URLS
            ),
            params=params,
        )
    except Exception:
        return []
    result = _first_chart_result(payload)
    if result is None:
        return []
    return _bars_from_chart_result(asset, result, interval)


def _bars_from_chart_result(
    asset: AssetConfig,
    result: dict[str, Any],
    interval: str,
) -> list[Bar]:
    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list) or not timestamps:
        return []
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    quote = quotes[0] if isinstance(quotes, list) and quotes else None
    if not isinstance(quote, dict):
        return []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars: list[Bar] = []
    for index, raw_ts in enumerate(timestamps):
        ts_number = _number(raw_ts)
        if ts_number is None:
            continue
        open_ = _number(opens[index]) if index < len(opens) else None
        high = _number(highs[index]) if index < len(highs) else None
        low = _number(lows[index]) if index < len(lows) else None
        close = _number(closes[index]) if index < len(closes) else None
        if open_ is None or high is None or low is None or close is None:
            continue
        bars.append(
            Bar(
                symbol=asset.symbol,
                provider="yahoo",
                interval=interval,
                timestamp=datetime.fromtimestamp(int(ts_number), UTC),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=_number(volumes[index]) if index < len(volumes) else None,
            )
        )
    return bars


def _bars_with_usd_display(
    asset: AssetConfig,
    bars: list[Bar],
    interval: str,
    range_: str,
) -> list[Bar]:
    currency = _asset_listing_currency(asset)
    fx_symbol = YAHOO_USD_FX_SYMBOLS.get(currency or "")
    if not bars or fx_symbol is None:
        return bars
    fx_bars = _get_raw_history_sync(
        AssetConfig(symbol=fx_symbol, type="index_proxy", source="yahoo"),
        interval,
        range_,
    )
    fx_rates = _fx_rates(fx_bars)
    if not fx_rates:
        return bars
    return [_bar_to_usd(bar, _matching_fx_rate(bar.timestamp, fx_rates)) for bar in bars]


def _asset_listing_currency(asset: AssetConfig) -> str | None:
    if asset.exchange == "KRX" or asset.symbol.upper().endswith(".KS"):
        return "KRW"
    return None


def _fx_rates(fx_bars: list[Bar]) -> list[tuple[datetime, float]]:
    return [
        (bar.timestamp, bar.close)
        for bar in fx_bars
        if bar.close > 0 and math.isfinite(bar.close)
    ]


def _matching_fx_rate(timestamp: datetime, rates: list[tuple[datetime, float]]) -> float:
    timestamps = [item[0] for item in rates]
    index = bisect.bisect_left(timestamps, timestamp)
    candidates = []
    if index < len(rates):
        candidates.append(rates[index])
    if index > 0:
        candidates.append(rates[index - 1])
    if not candidates:
        candidates.append(rates[0])
    return min(candidates, key=lambda item: abs((item[0] - timestamp).total_seconds()))[1]


def _bar_to_usd(bar: Bar, fx_rate: float) -> Bar:
    if fx_rate <= 0:
        return bar
    return Bar(
        symbol=bar.symbol,
        provider=bar.provider,
        interval=bar.interval,
        timestamp=bar.timestamp,
        open=round(bar.open / fx_rate, 6),
        high=round(bar.high / fx_rate, 6),
        low=round(bar.low / fx_rate, 6),
        close=round(bar.close / fx_rate, 6),
        volume=bar.volume,
    )


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


def _quote_with_usd_display(quote: Quote, fx_quote: Quote | None) -> Quote:
    if quote.currency in (None, "USD") or fx_quote is None or fx_quote.last <= 0:
        return quote
    display_last = quote.last / fx_quote.last
    display_previous_close = None
    if quote.previous_close is not None:
        fx_previous_close = (
            fx_quote.previous_close
            if fx_quote.previous_close is not None and fx_quote.previous_close > 0
            else fx_quote.last
        )
        display_previous_close = quote.previous_close / fx_previous_close
    display_change_abs = None
    display_change_pct = None
    if display_previous_close is not None and display_previous_close != 0:
        display_change_abs = round(display_last - display_previous_close, 6)
        display_change_pct = round(display_change_abs / display_previous_close * 100, 6)
    return replace(
        quote,
        display_last=display_last,
        display_previous_close=display_previous_close,
        display_change_abs=display_change_abs,
        display_change_pct=display_change_pct,
        display_currency="USD",
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
    """Map board ranges to chart-API ranges.

    Daily ranges over-fetch to 2y: the extra bars land in the SQLite cache and
    feed 200DMA / 52-week metrics on the daily board, and the history service
    trims the response back to the requested range.
    """
    return {
        "10m": "1d",
        "30m": "1d",
        "1h": "1d",
        "4h": "1d",
        "1d": "1d",
        "1w": "5d",
        "1mo": "1mo",
        "3mo": "2y",
        "ytd": "2y",
        "1y": "2y",
        "5y": "5y",
    }.get(range_, range_)
