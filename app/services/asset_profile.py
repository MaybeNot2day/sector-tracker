from __future__ import annotations

import math
import time
from typing import Any

# yfinance (and its pandas dependency) is imported lazily inside the two
# call sites: importing it at module level adds ~1s+ to serverless cold
# starts for a dependency only the profile endpoint uses.
from app.models import AssetConfig

USD_FX_SYMBOLS = {
    "KRW": "KRW=X",
}


class AssetProfileService:
    def __init__(self, *, cache_seconds: int = 3600) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict[str, object]]] = {}

    def get_profile(self, asset: AssetConfig) -> dict[str, object]:
        cached = self._cache.get(asset.symbol)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        payload = _base_profile(asset)
        if asset.source == "yahoo" and asset.type in {"equity", "etf", "index_proxy"}:
            try:
                import yfinance as yf

                info = yf.Ticker(asset.symbol).get_info()
                if isinstance(info, dict):
                    payload = _profile_from_yahoo_info(asset, info)
            except Exception:
                if cached and _is_cacheable_profile(cached[1]):
                    return cached[1]
                payload["status"] = "partial"

        if _is_cacheable_profile(payload):
            self._cache[asset.symbol] = (time.monotonic(), payload)
        return payload


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
    money_divisor = _usd_money_divisor(info)
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
            _etf_metrics(info, money_divisor)
            if is_etf
            else _equity_metrics(info, money_divisor)
        ),
    }


def _equity_metrics(
    info: dict[str, Any],
    money_divisor: float | None = 1.0,
) -> list[dict[str, object]]:
    rows = [
        _metric("Market Cap", _format_compact(_number(info, "marketCap"), money_divisor)),
        _metric("EV", _format_compact(_number(info, "enterpriseValue"), money_divisor)),
        _metric("P/E", _format_ratio(_number(info, "trailingPE"))),
        _metric("Forward P/E", _format_ratio(_number(info, "forwardPE"))),
        _metric("Price / Book", _format_ratio(_number(info, "priceToBook"))),
        _metric("Revenue", _format_compact(_number(info, "totalRevenue"), money_divisor)),
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
                money_divisor,
            ),
        ),
    ]
    return [row for row in rows if row["value"] is not None]


def _etf_metrics(
    info: dict[str, Any],
    money_divisor: float | None = 1.0,
) -> list[dict[str, object]]:
    rows = [
        _metric("Assets", _format_compact(_number(info, "totalAssets"), money_divisor)),
        _metric("NAV", _format_price(_number(info, "navPrice"), money_divisor)),
        _metric("Yield", _format_percent(_number(info, "yield"))),
        _metric("Expense", _format_percent(_number(info, "annualReportExpenseRatio"))),
        _metric("Beta 3Y", _format_ratio(_number(info, "beta3Year"))),
        _metric("Avg Volume", _format_plain_compact(_number(info, "averageVolume"))),
        _metric("52W High", _format_price(_number(info, "fiftyTwoWeekHigh"), money_divisor)),
        _metric("52W Low", _format_price(_number(info, "fiftyTwoWeekLow"), money_divisor)),
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


def _usd_money_divisor(info: dict[str, Any]) -> float | None:
    currency = _profile_currency(info)
    if currency in (None, "USD"):
        return 1.0
    symbol = USD_FX_SYMBOLS.get(currency)
    if symbol is None:
        return None
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
    except Exception:
        return None
    if df.empty:
        return None
    closes = [_coerce_number(value) for value in df["Close"].tolist()]
    closes = [value for value in closes if value is not None and value > 0]
    return closes[-1] if closes else None


def _profile_currency(info: dict[str, Any]) -> str | None:
    for key in ("currency", "financialCurrency"):
        value = info.get(key)
        if isinstance(value, str):
            cleaned = value.strip().upper()
            if cleaned:
                return cleaned
    return None


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


def _format_price(value: float | None, divisor: float | None = 1.0) -> str | None:
    if value is None or value <= 0:
        return None
    if divisor is None or divisor <= 0:
        return None
    value = value / divisor
    if abs(value) >= 1000:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def _format_price_range(
    low: float | None,
    high: float | None,
    divisor: float | None = 1.0,
) -> str | None:
    low_text = _format_price(low, divisor)
    high_text = _format_price(high, divisor)
    if low_text and high_text:
        return f"{low_text} - {high_text}"
    return None


def _format_percent(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value * 100:.1f}%"


def _format_compact(value: float | None, divisor: float | None = 1.0) -> str | None:
    if value is None:
        return None
    if divisor is None or divisor <= 0:
        return None
    value = value / divisor
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= 1_000_000_000_000:
        return f"{sign}${abs_value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{sign}${abs_value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{sign}${abs_value / 1_000_000:.1f}M"
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
