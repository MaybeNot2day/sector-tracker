from datetime import UTC, datetime

from app.models import AssetConfig, Bar, Quote
from app.providers.yahoo import (
    _bar_to_usd,
    _quote_from_chart_result,
    _quote_with_usd_display,
    _quotes_from_spark_payload,
)


def test_quote_from_chart_result_uses_latest_market_price() -> None:
    asset = AssetConfig(symbol="XME", type="etf", source="yahoo")
    result = {
        "meta": {
            "regularMarketPrice": 100.0,
            "regularMarketTime": 1_788_000_000,
            "postMarketPrice": 102.0,
            "postMarketTime": 1_788_000_300,
            "chartPreviousClose": 98.0,
            "currency": "USD",
        },
        "indicators": {"quote": [{"close": [99.0, 100.0]}]},
    }

    quote = _quote_from_chart_result(asset, result)

    assert quote is not None
    assert quote.symbol == "XME"
    assert quote.last == 102.0
    assert quote.previous_close == 98.0
    assert quote.change_pct == 4.081633
    assert quote.timestamp == datetime.fromtimestamp(1_788_000_300, UTC)
    assert quote.currency == "USD"


def test_quote_from_chart_result_falls_back_to_last_close() -> None:
    asset = AssetConfig(symbol="XBI", type="etf", source="yahoo")
    result = {
        "meta": {"chartPreviousClose": 50.0, "currency": "krw"},
        "indicators": {"quote": [{"close": [None, 51.0, 52.0]}]},
    }

    quote = _quote_from_chart_result(asset, result)

    assert quote is not None
    assert quote.last == 52.0
    assert quote.change_pct == 4.0
    assert quote.currency == "KRW"


def test_quotes_from_spark_payload_maps_responses_to_assets() -> None:
    asset = AssetConfig(symbol="XLU", type="etf", source="yahoo")
    payload = {
        "spark": {
            "result": [
                {
                    "symbol": "XLU",
                    "response": [
                        {
                            "meta": {
                                "regularMarketPrice": 45.5,
                                "regularMarketTime": 1_788_000_000,
                                "previousClose": 45.0,
                            }
                        }
                    ],
                }
            ]
        }
    }

    quotes = _quotes_from_spark_payload({"XLU": asset}, payload)

    assert quotes["XLU"].last == 45.5
    assert quotes["XLU"].change_pct == 1.111111


def test_quote_with_usd_display_converts_foreign_quote() -> None:
    quote = Quote.from_last_and_prev_close(
        symbol="005930.KS",
        asset_type="equity",
        provider="yahoo",
        last=314_500,
        previous_close=334_000,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        currency="KRW",
    )
    fx_quote = Quote.from_last_and_prev_close(
        symbol="KRW=X",
        asset_type="index_proxy",
        provider="yahoo",
        last=1_550,
        previous_close=1_540,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        currency="KRW",
    )

    converted = _quote_with_usd_display(quote, fx_quote)

    assert converted.last == 314_500
    assert converted.currency == "KRW"
    assert converted.display_currency == "USD"
    assert converted.display_last == 202.90322580645162
    assert converted.display_previous_close == 216.88311688311688
    assert converted.display_change_abs == -13.979891
    assert converted.display_change_pct == -6.445818


def test_bar_to_usd_converts_ohlc_and_keeps_volume() -> None:
    bar = Bar(
        symbol="000660.KS",
        provider="yahoo",
        interval="1d",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        open=2_480_000,
        high=2_600_000,
        low=2_450_000,
        close=2_560_000,
        volume=5_100_000,
    )

    converted = _bar_to_usd(bar, 1_550)

    assert converted.symbol == "000660.KS"
    assert converted.open == 1600.0
    assert converted.high == 1677.419355
    assert converted.low == 1580.645161
    assert converted.close == 1651.612903
    assert converted.volume == 5_100_000
