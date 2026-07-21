from time import monotonic
from typing import Any

import httpx
import pytest

from app.providers.lighter import LighterProvider, _is_crypto_detail
from app.services.daily_board import crypto_breadth_metrics


def forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to build an HTTP client fails the test."""

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("HTTP client constructed during a cached call")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)


def seeded_provider(details: dict[str, dict[str, Any]]) -> LighterProvider:
    """Provider with a warm details cache, so lookups never hit HTTP."""
    provider = LighterProvider()
    provider._details = details
    provider._details_time = monotonic()
    return provider


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        pytest.param({"symbol": "BTC", "strategy_index": 2}, True, id="crypto-perp-bucket"),
        pytest.param({"symbol": "PEPE", "strategy_index": 0}, True, id="legacy-meme-bucket"),
        pytest.param({"symbol": "MAGS", "strategy_index": 0}, False, id="legacy-tradfi-symbol"),
        pytest.param({"symbol": "XAU", "strategy_index": 3}, False, id="commodity-bucket"),
        pytest.param({"symbol": "EURUSD", "strategy_index": 4}, False, id="fx-bucket"),
        pytest.param({"symbol": "AAPL", "strategy_index": 5}, False, id="us-equity-bucket"),
        pytest.param({"symbol": "SAMSUNG", "strategy_index": 6}, False, id="asia-equity-bucket"),
        pytest.param({"symbol": "OPENAI", "strategy_index": 7}, False, id="pre-ipo-bucket"),
        pytest.param({"symbol": "BTC"}, True, id="missing-strategy-crypto-symbol"),
        pytest.param({"symbol": "MAGS"}, False, id="missing-strategy-legacy-tradfi"),
        pytest.param(
            {"symbol": "BTC", "strategy_index": "5"}, True, id="non-int-strategy-symbol-wins"
        ),
        pytest.param(
            {"symbol": "spacex", "strategy_index": None}, False, id="legacy-symbol-case-folded"
        ),
    ],
)
def test_is_crypto_detail_classification(detail: dict[str, Any], expected: bool) -> None:
    assert _is_crypto_detail(detail) is expected


def tape_details() -> dict[str, dict[str, Any]]:
    return {
        "BTC": {
            "symbol": "BTC",
            "strategy_index": 2,
            "status": "active",
            "market_id": 1,
            "last_trade_price": 62000.0,
            "daily_price_change": 0.59,
            "open_interest": 1729.9,
            "daily_quote_token_volume": 250_000_000.0,
        },
        # Lighter's API serves numbers as strings; the tape must parse them.
        "ETH": {
            "symbol": "ETH",
            "strategy_index": 2,
            "status": "active",
            "market_id": 2,
            "last_trade_price": "2450.5",
            "daily_price_change": -3.75,
            "open_interest": 1000.0,
            "daily_quote_token_volume": "300000000.0",
        },
        # 10.3333 * 147.0 = 1518.9951 -> rounds to 1519.0, not the raw product.
        "SOL": {
            "symbol": "SOL",
            "strategy_index": 2,
            "status": "active",
            "market_id": 3,
            "last_trade_price": 147.0,
            "daily_price_change": 1.1,
            "open_interest": 10.3333,
            "daily_quote_token_volume": 1_000_000.0,
        },
        # Legacy strategy 0 meme coin: included; carries the missing-field duties.
        "PEPE": {
            "symbol": "PEPE",
            "strategy_index": 0,
            "status": "active",
            "market_id": 4,
            "last_trade_price": 0.0000112,
            "daily_price_change": -100.0,
        },
        # Excluded: legacy strategy 0 bucket but a known TradFi listing.
        "MAGS": {
            "symbol": "MAGS",
            "strategy_index": 0,
            "status": "active",
            "market_id": 5,
            "last_trade_price": 55.0,
            "daily_price_change": 2.0,
            "daily_quote_token_volume": 9_000_000.0,
        },
        # Excluded: TradFi strategy bucket.
        "AAPL": {
            "symbol": "AAPL",
            "strategy_index": 5,
            "status": "active",
            "market_id": 6,
            "last_trade_price": 212.5,
            "daily_price_change": 1.25,
            "daily_quote_token_volume": 8_000_000.0,
        },
        # Excluded: zero, negative, and missing last trade price.
        "HALTED": {
            "symbol": "HALTED",
            "strategy_index": 2,
            "status": "active",
            "market_id": 7,
            "last_trade_price": 0.0,
            "daily_quote_token_volume": 7_000_000.0,
        },
        "NEGP": {
            "symbol": "NEGP",
            "strategy_index": 2,
            "status": "active",
            "market_id": 8,
            "last_trade_price": -5.0,
        },
        "NOPX": {
            "symbol": "NOPX",
            "strategy_index": 2,
            "status": "active",
            "market_id": 9,
        },
    }


def test_crypto_tape_cached_builds_sorted_rows_from_caches_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = seeded_provider(tape_details())
    provider._funding = {"BTC": 1.2e-05, "ETH": -2.5e-06, "SOL": 0.0}

    tape = provider.crypto_tape_cached()

    # Volume-descending; the missing-volume row sorts last.
    assert [row["symbol"] for row in tape] == ["ETH", "BTC", "SOL", "PEPE"]

    assert tape[1] == {
        "symbol": "BTC",
        "basket": "L1",  # cold live cache falls back to the baked-in snapshot
        "last": 62000.0,
        "change_pct": 0.59,
        "funding_rate": 1.2e-05,
        "open_interest_usd": 107_253_800.0,
        "day_volume_usd": 250_000_000.0,
    }

    eth = tape[0]
    assert eth["last"] == 2450.5  # parsed from the string payload
    assert eth["day_volume_usd"] == 300_000_000.0

    sol = tape[2]
    assert sol["open_interest_usd"] == 1519.0  # round(10.3333 * 147.0, 2)

    pepe = tape[3]
    assert pepe["change_pct"] == -100.0  # verbatim, unlike the quote path
    assert pepe["funding_rate"] is None
    assert pepe["open_interest_usd"] is None
    assert pepe["day_volume_usd"] is None


def test_crypto_tape_cached_is_empty_when_cache_is_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)

    assert LighterProvider().crypto_tape_cached() == []


def test_is_crypto_market_answers_from_cache_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = seeded_provider(
        {
            "BTC": {"symbol": "BTC", "strategy_index": 2, "market_id": 1},
            "AAPL": {"symbol": "AAPL", "strategy_index": 5, "market_id": 2},
            "MAGS": {"symbol": "MAGS", "strategy_index": 0, "market_id": 3},
        }
    )
    # Even a stale cache answers: the call must never refresh.
    provider._details_time = 0.0

    assert provider.is_crypto_market("BTC") is True
    assert provider.is_crypto_market("btc") is True
    assert provider.is_crypto_market("AAPL") is False
    assert provider.is_crypto_market("mags") is False
    assert provider.is_crypto_market("DOGE") is False


def test_crypto_breadth_metrics_counts_boundaries_and_ignores_non_numeric() -> None:
    tape: list[dict[str, object]] = [
        {"symbol": "UP10", "change_pct": 10.0, "funding_rate": 0.0001, "day_volume_usd": 1000.13},
        {"symbol": "UP3", "change_pct": 3.0, "funding_rate": -0.0002, "day_volume_usd": 250.12},
        {"symbol": "UPNEAR3", "change_pct": 2.9, "funding_rate": 0.0, "day_volume_usd": None},
        {"symbol": "UPSMALL", "change_pct": 1.23456, "funding_rate": None},
        {"symbol": "DOWNSMALL", "change_pct": -0.5, "funding_rate": "broken"},
        {"symbol": "DOWN3", "change_pct": -3.0, "day_volume_usd": "n/a"},
        {"symbol": "DOWN10", "change_pct": -10.0},
        {"symbol": "UNQUOTED", "change_pct": None},
        {"symbol": "STRINGY", "change_pct": "4.2"},
    ]

    assert crypto_breadth_metrics(tape) == {
        "total": 9,
        "quoted": 7,
        "advancers": 4,
        "decliners": 3,
        "advance_pct": 57.1,  # round(4 / 7 * 100, 1)
        "up_3pct": 2,  # 3.0 counts, 2.9 does not
        "down_3pct": 2,
        "up_10pct": 1,
        "down_10pct": 1,
        "median_change": 1.2346,  # median of 7 numeric changes, 4dp
        "volume_usd": 1250.25,
        "positive_funding_pct": 33.3,  # 1 of 3 numeric rates; strings ignored
    }


def test_crypto_breadth_metrics_empty_tape() -> None:
    assert crypto_breadth_metrics([]) == {
        "total": 0,
        "quoted": 0,
        "advancers": 0,
        "decliners": 0,
        "advance_pct": None,
        "up_3pct": 0,
        "down_3pct": 0,
        "up_10pct": 0,
        "down_10pct": 0,
        "median_change": None,
        "volume_usd": None,
        "positive_funding_pct": None,
    }


def test_crypto_breadth_metrics_volume_without_quotes() -> None:
    """None means "no data", never conflated with a zero count."""
    tape: list[dict[str, object]] = [
        {"symbol": "A", "change_pct": None, "day_volume_usd": 40.0},
        {"symbol": "B", "day_volume_usd": 2.5},
    ]

    metrics = crypto_breadth_metrics(tape)

    assert metrics["total"] == 2
    assert metrics["quoted"] == 0
    assert metrics["advance_pct"] is None
    assert metrics["median_change"] is None
    assert metrics["positive_funding_pct"] is None
    assert metrics["volume_usd"] == 42.5
