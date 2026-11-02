from __future__ import annotations

import asyncio
import math
import time
from typing import Any, NamedTuple

# yfinance (and its pandas dependency) is imported lazily inside the two
# call sites: importing it at module level adds ~1s+ to serverless cold
# starts for a dependency only the profile endpoint uses.
from app.models import AssetConfig


class _Money(NamedTuple):
    """How to render one money field: divide by `divisor`, prefix with `prefix`."""

    divisor: float
    prefix: str


_USD = _Money(1.0, "$")


class AssetProfileService:
    def __init__(
        self,
        *,
        cache_seconds: int = 3600,
        failure_retry_seconds: int = 120,
    ) -> None:
        self.cache_seconds = cache_seconds
        self.failure_retry_seconds = failure_retry_seconds
        self._cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._failures: dict[str, tuple[float, dict[str, object]]] = {}
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    async def get_profile(self, asset: AssetConfig) -> dict[str, object]:
        # A per-symbol asyncio.Lock collapses concurrent cold misses without
        # serializing other symbols; only the actual yfinance fetch occupies
        # an executor thread — waiters park on the event loop instead of
        # pinning a thread each behind a threading.Lock.
        lock = self._symbol_locks.setdefault(asset.symbol, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._get_profile_locked, asset)

    def _get_profile_locked(self, asset: AssetConfig) -> dict[str, object]:
        now = time.monotonic()
        cached = self._cache.get(asset.symbol)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]

        failure = self._failures.get(asset.symbol)
        if (
            failure
            and self.failure_retry_seconds > 0
            and now - failure[0] < self.failure_retry_seconds
        ):
            return cached[1] if cached and _is_cacheable_profile(cached[1]) else failure[1]

        payload = _base_profile(asset)
        if asset.source != "yahoo" or asset.type not in {"equity", "etf", "index_proxy"}:
            return payload

        try:
            import yfinance as yf

            info = yf.Ticker(asset.symbol).get_info()
            if isinstance(info, dict):
                payload = _profile_from_yahoo_info(asset, info)
        except Exception:
            payload["status"] = "partial"

        now = time.monotonic()
        if _is_cacheable_profile(payload):
            self._cache[asset.symbol] = (now, payload)
            self._failures.pop(asset.symbol, None)
            return payload

        # Keep a stale good profile through an outage; otherwise retain the
        # partial payload briefly so repeated requests do not hammer Yahoo.
        fallback = cached[1] if cached and _is_cacheable_profile(cached[1]) else payload
        self._failures[asset.symbol] = (now, fallback)
        return fallback


def _base_profile(asset: AssetConfig) -> dict[str, object]:
    return {
        "status": "partial",
        "symbol": asset.symbol,
        "name": asset.name,
        "asset_type": asset.type,
        "source": asset.source,
        "exchange": asset.exchange,
        "sector": None,
        "industry": None,
        "website": None,
        "description": None,
        "metrics": [],
    }


def _profile_from_yahoo_info(asset: AssetConfig, info: dict[str, Any]) -> dict[str, object]:
    quote_type = _text(info, "quoteType")
    is_etf = asset.type == "etf" or quote_type == "ETF"
    # ADRs trade in USD but Yahoo reports statements (revenue, EV) in the
    # issuer's financialCurrency (TSM: TWD, BABA: CNY); each field converts
    # from its own currency.
    quote_currency = _info_currency(info, "currency")
    statement_currency = _info_currency(info, "financialCurrency") or quote_currency
    rates: dict[str, _Money] = {}

    def money(currency: str | None) -> _Money:
        if currency is None or currency == "USD":
            return _USD
        if currency not in rates:
            rates[currency] = _usd_money(currency)
        return rates[currency]

    quote_money = money(quote_currency)
    return {
        "status": "ok",
        "symbol": asset.symbol,
        "name": _text(info, "longName", "shortName") or asset.name,
        "asset_type": asset.type,
        "source": asset.source,
        "exchange": _text(info, "exchange") or asset.exchange,
        "sector": _text(info, "sector") or ("ETF" if is_etf else None),
        "industry": _text(info, "industry", "category"),
        "website": _text(info, "website"),
        "description": _text(info, "longBusinessSummary"),
        "metrics": (
            _etf_metrics(info, quote_money)
            if is_etf
            else _equity_metrics(info, quote_money, money(statement_currency))
        ),
    }


def _equity_metrics(
    info: dict[str, Any],
    quote_money: _Money = _USD,
    statement_money: _Money = _USD,
) -> list[dict[str, object]]:
    rows = [
        _metric("Market Cap", _format_compact(_number(info, "marketCap"), quote_money)),
        _metric("EV", _format_compact(_number(info, "enterpriseValue"), statement_money)),
        _metric("P/E", _format_ratio(_number(info, "trailingPE"))),
        _metric("Forward P/E", _format_ratio(_number(info, "forwardPE"))),
        _metric("Price / Book", _format_ratio(_number(info, "priceToBook"))),
        _metric("Revenue", _format_compact(_number(info, "totalRevenue"), statement_money)),
        _metric("Gross Margin", _format_percent(_number(info, "grossMargins"))),
        _metric("Profit Margin", _format_percent(_number(info, "profitMargins"))),
        _metric("Revenue Growth", _format_percent(_number(info, "revenueGrowth"))),
        _metric("Beta", _format_ratio(_number(info, "beta"))),
        _metric("Avg Volume", _format_plain_compact(_number(info, "averageVolume"))),
        _metric(
            "52W Range",
            _format_price_range(
                _number(info, "fiftyTwoWeekLow"),
                _number(info, "fiftyTwoWeekHigh"),
                quote_money,
            ),
        ),
    ]
    return [row for row in rows if row["value"] is not None]


def _etf_metrics(
    info: dict[str, Any],
    money: _Money = _USD,
) -> list[dict[str, object]]:
    rows = [
        _metric("Assets", _format_compact(_number(info, "totalAssets"), money)),
        _metric("NAV", _format_price(_number(info, "navPrice"), money)),
        _metric("Yield", _format_percent(_number(info, "yield"))),
        _metric("Expense", _format_percent(_number(info, "annualReportExpenseRatio"))),
        _metric("Beta 3Y", _format_ratio(_number(info, "beta3Year"))),
        _metric("Avg Volume", _format_plain_compact(_number(info, "averageVolume"))),
        _metric("52W High", _format_price(_number(info, "fiftyTwoWeekHigh"), money)),
        _metric("52W Low", _format_price(_number(info, "fiftyTwoWeekLow"), money)),
    ]
    return [row for row in rows if row["value"] is not None]


def _metric(label: str, value: str | None) -> dict[str, object]:
    return {"label": label, "value": value}


def _is_cacheable_profile(payload: dict[str, object]) -> bool:
    if payload.get("status") != "ok":
        return False
    return bool(payload.get("description") or payload.get("metrics"))


def _text(info: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = info.get(key)
        if isinstance(value, str):
            cleaned = " ".join(value.split())
            if cleaned:
                return cleaned
    return None


def _number(info: dict[str, Any], key: str) -> float | None:
    value = info.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return float(value)
    return None


def _usd_money(currency: str) -> _Money:
    """USD conversion for `currency`; without an FX rate, label the real currency."""
    rate = _usd_rate(currency)
    if rate is None:
        return _Money(1.0, f"{currency} ")
    return _Money(rate, "$")


def _usd_rate(currency: str) -> float | None:
    """Units of `currency` per USD from Yahoo's `XXX=X` FX series."""
    # Minor units (GBp, ZAc, ILA) are not ISO majors and have no =X pair.
    if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        return None
    try:
        import yfinance as yf

        df = yf.Ticker(f"{currency}=X").history(period="5d", interval="1d", auto_adjust=False)
    except Exception:
        return None
    if df.empty:
        return None
    closes = [_coerce_number(value) for value in df["Close"].tolist()]
    closes = [value for value in closes if value is not None and value > 0]
    return closes[-1] if closes else None


def _info_currency(info: dict[str, Any], key: str) -> str | None:
    value = info.get(key)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    # Only alphabetic codes reach the label; anything else is malformed.
    return cleaned if cleaned.isalpha() and len(cleaned) <= 4 else None


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _format_ratio(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


def _format_price(value: float | None, money: _Money = _USD) -> str | None:
    if value is None or value <= 0:
        return None
    value = value / money.divisor
    if abs(value) >= 1000:
        return f"{money.prefix}{value:,.0f}"
    return f"{money.prefix}{value:,.2f}"


def _format_price_range(
    low: float | None,
    high: float | None,
    money: _Money = _USD,
) -> str | None:
    low_text = _format_price(low, money)
    high_text = _format_price(high, money)
    if low_text and high_text:
        return f"{low_text} - {high_text}"
    return None


def _format_percent(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value * 100:.1f}%"


def _format_compact(value: float | None, money: _Money = _USD) -> str | None:
    if value is None:
        return None
    value = value / money.divisor
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    prefix = money.prefix
    if abs_value >= 1_000_000_000_000:
        return f"{sign}{prefix}{abs_value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{sign}{prefix}{abs_value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{sign}{prefix}{abs_value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{sign}{abs_value / 1_000:.1f}K"
    return f"{sign}{abs_value:.0f}"


def _format_plain_compact(value: float | None) -> str | None:
    if value is None:
        return None
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= 1_000_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{sign}{abs_value / 1_000:.1f}K"
    return f"{sign}{abs_value:.0f}"
