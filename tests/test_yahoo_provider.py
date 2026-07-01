from datetime import UTC, datetime

from app.models import AssetConfig
from app.providers.yahoo import _quote_from_chart_result, _quotes_from_spark_payload


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
