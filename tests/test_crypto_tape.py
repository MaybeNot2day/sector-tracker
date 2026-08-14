from time import monotonic
from typing import Any

import httpx
import pytest

from app.providers.hyperliquid import HyperliquidProvider, _parse_universe
from app.services.daily_board import crypto_breadth_metrics


def forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to build an HTTP client fails the test."""

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("HTTP client constructed during a cached call")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)


def seeded_provider(payload: list[Any]) -> HyperliquidProvider:
    """Provider with a warm crypto market map, so lookups never hit HTTP."""
    provider = HyperliquidProvider()
    provider._crypto = _parse_universe(payload, strip_prefix=None)
    provider._crypto_time = monotonic()
    return provider


def tape_payload() -> list[Any]:
    """A main-dex metaAndAssetCtxs response; universe[i] pairs with ctxs[i]."""
    universe = [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "ETH"},
        {"name": "SOL"},
        # Meme wrapper with a sparse ctx: carries the missing-field duties.
        {"name": "kPEPE"},
        # Excluded: delisted market.
        {"name": "OLD", "isDelisted": True},
        # Excluded: zero, negative, and missing mark price.
        {"name": "HALTED"},
        {"name": "NEGP"},
        {"name": "NOPX"},
    ]
    # Hyperliquid serves every number as a string; the tape must parse them.
    ctxs = [
        {
            "markPx": "62000.0",
            "prevDayPx": "61000.0",
            "funding": "0.0000125",
            "openInterest": "1729.9",
            "dayNtlVlm": "250000000.0",
            "dayBaseVlm": "4032.5",
        },
        {"markPx": "2450.5", "prevDayPx": "2500.0", "dayNtlVlm": "300000000.0"},
        # 10.3333 * 147.0 = 1518.9951 -> rounds to 1519.0, not the raw product.
        {
            "markPx": "147.0",
            "prevDayPx": "147.0",
            "openInterest": "10.3333",
            "dayNtlVlm": "1000000.0",
        },
        {"markPx": "0.0000112"},
        {"markPx": "1.0", "prevDayPx": "1.0"},
        {"markPx": "0.0", "dayNtlVlm": "7000000.0"},
        {"markPx": "-5.0"},
        {},
    ]
    return [{"universe": universe}, ctxs]


def test_crypto_tape_cached_builds_sorted_rows_from_caches_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = seeded_provider(tape_payload())
    # TradFi synthetics live in a separate map and never reach the tape.
    provider._tradfi = _parse_universe(
        [
            {"universe": [{"name": "xyz:AAPL"}]},
            [{"markPx": "212.5", "prevDayPx": "210.0", "dayNtlVlm": "8000000.0"}],
        ],
        strip_prefix="xyz:",
    )

    tape = provider.crypto_tape_cached()

    # Volume-descending; the missing-volume row sorts last.
    assert [row["symbol"] for row in tape] == ["ETH", "BTC", "SOL", "kPEPE"]

    assert tape[1] == {
        "symbol": "BTC",
        "basket": "L1",  # from the baked-in category snapshot
        "last": 62000.0,
        "change_pct": 1.639344,  # round((62000 - 61000) / 61000 * 100, 6)
        "funding_rate": 1.25e-05,  # the hourly fraction, straight from the ctx
        "open_interest_usd": 107_253_800.0,  # round(1729.9 * 62000.0, 2)
        "day_volume_usd": 250_000_000.0,
    }

    eth = tape[0]
    assert eth["last"] == 2450.5  # parsed from the string payload
    assert eth["change_pct"] == -1.98
    assert eth["day_volume_usd"] == 300_000_000.0

    sol = tape[2]
    assert sol["open_interest_usd"] == 1519.0  # round(10.3333 * 147.0, 2)

    pepe = tape[3]
    assert pepe["symbol"] == "kPEPE"  # the API coin name, displayed verbatim
    assert pepe["basket"] == "Memes"
    assert pepe["change_pct"] is None  # no prevDayPx
    assert pepe["funding_rate"] is None
    assert pepe["open_interest_usd"] is None
    assert pepe["day_volume_usd"] is None


def test_crypto_tape_cached_is_empty_when_cache_is_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)

    assert HyperliquidProvider().crypto_tape_cached() == []


def test_is_crypto_market_answers_from_cache_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = HyperliquidProvider()
    provider._crypto = {
        "BTC": {"coin": "BTC", "display": "BTC", "last": 62000.0},
        # Token side of a ticker collision with the ROBO ETF.
        "ROBO": {"coin": "ROBO", "display": "ROBO", "last": 0.014},
    }
    provider._tradfi = {
        "AAPL": {"coin": "xyz:AAPL", "display": "AAPL", "last": 212.5},
        "ROBO": {"coin": "xyz:ROBO", "display": "ROBO", "last": 83.4},
    }
    # Even a stale cache answers: the call must never refresh.
    provider._markets_time = 0.0

    assert provider.is_crypto_market("BTC") is True
    assert provider.is_crypto_market("btc") is True
    assert provider.is_crypto_market("AAPL") is False
    assert provider.is_crypto_market("DOGE") is False
    assert provider.is_tradfi_market("aapl") is True
    assert provider.is_tradfi_market("BTC") is False
    # A collision is listed on both sides; the maps never leak into each other.
    assert provider.is_crypto_market("ROBO") is True
    assert provider.is_tradfi_market("ROBO") is True


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
