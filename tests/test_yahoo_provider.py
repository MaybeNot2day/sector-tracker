from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.models import AssetConfig, Bar, Quote
from app.providers import yahoo as yahoo_module
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
    # Post print newer than the regular print -> Friday-close semantics: the
    # frozen regular price IS the last completed official session close.
    assert quote.official_close == 100.0


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


@pytest.fixture(autouse=True)
def reset_yahoo_cooldowns() -> Iterator[None]:
    yahoo_module._rate_limited_until.clear()
    yahoo_module._failure_until.clear()
    yield
    yahoo_module._rate_limited_until.clear()
    yahoo_module._failure_until.clear()


def test_successful_alternate_host_does_not_arm_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get_json(url: str, params: dict[str, str]) -> dict[str, object]:
        calls.append(url)
        if "query1" in url:
            raise RuntimeError("query1 unavailable")
        return {"host": "query2"}

    monkeypatch.setattr(yahoo_module, "_get_json", fake_get_json)
    monkeypatch.setattr(yahoo_module, "sleep", lambda seconds: None)

    payload = yahoo_module._get_json_with_retry(
        tuple(url.format(symbol="SPY") for url in yahoo_module.YAHOO_CHART_URLS),
        params={"range": "1d"},
    )

    assert payload == {"host": "query2"}
    assert len(calls) == 2
    assert "chart" not in yahoo_module._failure_until


def test_exhausted_failure_arms_symbol_cooldown_then_retries_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 1_000.0
    outage = True
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_get_json(url: str, params: dict[str, str]) -> dict[str, object]:
        calls.append(url)
        if yahoo_module._endpoint_family(url) == "spark":
            return {"family": "spark"}
        if outage:
            raise RuntimeError("chart outage")
        return {"family": "chart"}

    monkeypatch.setattr(yahoo_module, "monotonic", lambda: clock)
    monkeypatch.setattr(yahoo_module, "_get_json", fake_get_json)
    monkeypatch.setattr(yahoo_module, "sleep", sleeps.append)
    chart_urls = tuple(
        url.format(symbol="SPY") for url in yahoo_module.YAHOO_CHART_URLS
    )

    with pytest.raises(RuntimeError, match="chart outage"):
        yahoo_module._get_json_with_retry(chart_urls, params={"range": "1d"})

    assert calls == list(chart_urls) * 3
    assert sleeps == [1.5, 3.0]
    assert yahoo_module._failure_until == {"chart:SPY": 1_030.0}

    first_call_count = len(calls)
    with pytest.raises(
        yahoo_module.YahooFailureCooldown,
        match="chart:SPY cooling down after request failures",
    ):
        yahoo_module._get_json_with_retry(chart_urls, params={"range": "1d"})
    assert len(calls) == first_call_count

    # Spark is an independent endpoint family and remains callable.
    assert yahoo_module._get_json_with_retry(
        yahoo_module.YAHOO_SPARK_URLS,
        params={"symbols": "SPY"},
    ) == {"family": "spark"}

    outage = False
    clock = 1_031.0
    assert yahoo_module._get_json_with_retry(
        chart_urls,
        params={"range": "1d"},
    ) == {"family": "chart"}
    assert calls[-1] == chart_urls[0]


def test_one_symbol_chart_failure_does_not_cool_down_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_json(url: str, params: dict[str, str]) -> dict[str, object]:
        if url.endswith("/DEAD"):
            raise RuntimeError("404 delisted")
        return {"symbol": url.rsplit("/", 1)[-1]}

    monkeypatch.setattr(yahoo_module, "_get_json", fake_get_json)
    monkeypatch.setattr(yahoo_module, "sleep", lambda seconds: None)
    dead_urls = tuple(url.format(symbol="DEAD") for url in yahoo_module.YAHOO_CHART_URLS)
    spy_urls = tuple(url.format(symbol="SPY") for url in yahoo_module.YAHOO_CHART_URLS)

    with pytest.raises(RuntimeError, match="404 delisted"):
        yahoo_module._get_json_with_retry(dead_urls, params={"range": "1d"})

    # Only the delisted symbol cools down; every other chart call proceeds.
    assert set(yahoo_module._failure_until) == {"chart:DEAD"}
    assert yahoo_module._get_json_with_retry(spy_urls, params={"range": "1d"}) == {
        "symbol": "SPY"
    }
    with pytest.raises(yahoo_module.YahooFailureCooldown):
        yahoo_module._get_json_with_retry(dead_urls, params={"range": "1d"})


def test_official_close_is_previous_close_while_regular_session_trades() -> None:
    asset = AssetConfig(symbol="XME", type="etf", source="yahoo")
    result = {
        "meta": {
            "regularMarketPrice": 101.0,
            "regularMarketTime": 1_788_000_000,
            # This morning's pre-market print is OLDER than the regular one.
            "preMarketPrice": 99.5,
            "preMarketTime": 1_787_980_000,
            "previousClose": 98.0,
            "currentTradingPeriod": {
                "regular": {"start": 1_787_990_000, "end": 1_788_010_000}
            },
        },
    }

    quote = _quote_from_chart_result(asset, result)

    assert quote is not None
    assert quote.official_close == 98.0


def test_official_close_is_regular_close_when_session_period_has_ended() -> None:
    # Just after the bell, before any post-market print lands: the regular
    # print sits at currentTradingPeriod.regular.end, i.e. today's close.
    asset = AssetConfig(symbol="XME", type="etf", source="yahoo")
    result = {
        "meta": {
            "regularMarketPrice": 100.0,
            "regularMarketTime": 1_788_010_000,
            "previousClose": 98.0,
            "currentTradingPeriod": {
                "regular": {"start": 1_787_986_600, "end": 1_788_010_000}
            },
        },
    }

    quote = _quote_from_chart_result(asset, result)

    assert quote is not None
    assert quote.official_close == 100.0


def test_official_close_absent_without_session_signals() -> None:
    # Slim meta (no pre/post prints, no trading period) is undecidable; the
    # overlay then falls back to its freshness heuristic.
    asset = AssetConfig(symbol="XME", type="etf", source="yahoo")
    result = {
        "meta": {
            "regularMarketPrice": 100.0,
            "regularMarketTime": 1_788_000_000,
            "previousClose": 98.0,
        },
    }

    quote = _quote_from_chart_result(asset, result)

    assert quote is not None
    assert quote.official_close is None
