from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider
from app.services.quotes import QuoteService, _official_close


class ScriptedQuotes(QuoteProvider):
    name = "yahoo"

    def __init__(self, quotes: dict[str, Quote]) -> None:
        self._quotes = quotes

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return [self._quotes[asset.symbol] for asset in assets if asset.symbol in self._quotes]

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return []


def yahoo_quote(
    symbol: str,
    *,
    last: float = 210.0,
    previous_close: float | None = 208.0,
    timestamp: datetime | None = None,
    currency: str | None = "USD",
    volume: float | None = None,
    error: str | None = None,
) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol=symbol,
        asset_type="equity",
        provider="yahoo",
        last=last,
        previous_close=previous_close,
        timestamp=timestamp or datetime.now(UTC),
        currency=currency,
        volume=volume,
        error=error,
    )


def lighter_with(details: dict[str, dict[str, Any]]) -> LighterProvider:
    """Real LighterProvider with a warm details cache, so overlay never does HTTP."""
    provider = LighterProvider()
    provider._details = details
    provider._details_time = monotonic()
    return provider


def aapl_details(last_trade_price: float = 213.5) -> dict[str, dict[str, Any]]:
    return {
        "AAPL": {
            "symbol": "AAPL",
            "market_id": 42,
            "status": "active",
            "last_trade_price": last_trade_price,
        }
    }


def equity_group(*symbols: str) -> list[GroupConfig]:
    return [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol=s, type="equity", source="yahoo") for s in symbols],
        )
    ]


# --- _official_close -------------------------------------------------------


def test_official_close_uses_previous_close_while_session_is_live() -> None:
    now = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    quote = yahoo_quote("AAPL", last=210.0, previous_close=208.0,
                        timestamp=now - timedelta(minutes=5))

    assert _official_close(quote, now) == 208.0


def test_official_close_uses_last_print_after_hours() -> None:
    now = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    quote = yahoo_quote("AAPL", last=210.0, previous_close=208.0,
                        timestamp=now - timedelta(hours=2))

    assert _official_close(quote, now) == 210.0


def test_official_close_treats_naive_timestamps_as_utc() -> None:
    now = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    fresh_naive = (now - timedelta(minutes=5)).replace(tzinfo=None)
    stale_naive = (now - timedelta(hours=2)).replace(tzinfo=None)

    fresh = yahoo_quote("AAPL", last=210.0, previous_close=208.0, timestamp=fresh_naive)
    stale = yahoo_quote("AAPL", last=210.0, previous_close=208.0, timestamp=stale_naive)

    assert _official_close(fresh, now) == 208.0
    assert _official_close(stale, now) == 210.0


# --- overlay through _fetch_fresh_quotes ------------------------------------


@pytest.mark.asyncio
async def test_overlay_replaces_price_with_lighter_live_and_keeps_volume(
    tmp_path: Path,
) -> None:
    groups = equity_group("AAPL")
    yahoo = ScriptedQuotes(
        {"AAPL": yahoo_quote("AAPL", last=210.0, previous_close=208.0, volume=1_234_567.0)}
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "lighter": lighter_with(aapl_details(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    quote = fresh["AAPL"]
    assert quote.provider == "lighter"
    assert quote.last == 213.5
    # Session live (fresh venue quote) -> baseline is the venue previous close.
    assert quote.previous_close == 208.0
    assert quote.change_abs == pytest.approx(5.5)
    assert quote.change_pct == pytest.approx(2.644231)
    # Official-session share volume survives the overlay.
    assert quote.volume == 1_234_567.0
    assert quote.currency == "USD"


@pytest.mark.asyncio
async def test_overlay_baseline_is_venue_last_after_hours(tmp_path: Path) -> None:
    groups = equity_group("AAPL")
    stale_ts = datetime.now(UTC) - timedelta(hours=2)
    yahoo = ScriptedQuotes(
        {"AAPL": yahoo_quote("AAPL", last=210.0, previous_close=208.0, timestamp=stale_ts)}
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "lighter": lighter_with(aapl_details(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    quote = fresh["AAPL"]
    assert quote.provider == "lighter"
    assert quote.last == 213.5
    # Venue closed -> its final print is the official close baseline.
    assert quote.previous_close == 210.0
    assert quote.change_abs == pytest.approx(3.5)
    assert quote.change_pct == pytest.approx(1.666667)


@pytest.mark.asyncio
async def test_overlay_skips_non_usd_listings(tmp_path: Path) -> None:
    groups = equity_group("SMSN")
    original = yahoo_quote("SMSN", last=71000.0, previous_close=70500.0, currency="KRW")
    yahoo = ScriptedQuotes({"SMSN": original})
    lighter = lighter_with(
        {"SMSN": {"symbol": "SMSN", "market_id": 5, "status": "active",
                  "last_trade_price": 50.0}}
    )
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["SMSN"] == original


@pytest.mark.asyncio
async def test_overlay_skips_error_quotes(tmp_path: Path) -> None:
    groups = equity_group("AAPL")
    broken = yahoo_quote("AAPL", last=210.0, previous_close=208.0, error="upstream_down")
    yahoo = ScriptedQuotes({"AAPL": broken})
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "lighter": lighter_with(aapl_details(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["AAPL"] == broken


@pytest.mark.asyncio
async def test_overlay_leaves_symbols_without_lighter_market_untouched(
    tmp_path: Path,
) -> None:
    groups = equity_group("MSFT")
    original = yahoo_quote("MSFT", last=430.0, previous_close=425.0)
    yahoo = ScriptedQuotes({"MSFT": original})
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "lighter": lighter_with(aapl_details())},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["MSFT"] == original


class RecordingLighter(LighterProvider):
    def __init__(self, details: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self._details = details
        self._details_time = monotonic()
        self._funding_time = monotonic()  # keep funding warm: no HTTP for perps
        self.requested_candidates: set[str] | None = None

    async def live_prices(self, symbols: set[str]) -> dict[str, float]:
        self.requested_candidates = set(symbols)
        return await super().live_prices(symbols)


@pytest.mark.asyncio
async def test_overlay_candidates_exclude_lighter_sourced_and_crypto_assets(
    tmp_path: Path,
) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[
                AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
                AssetConfig(symbol="XLE", type="etf", source="yahoo"),
                AssetConfig(symbol="BTC", type="crypto_perp", source="lighter"),
                AssetConfig(symbol="SYN", type="equity", source="lighter"),
            ],
        )
    ]
    yahoo = ScriptedQuotes(
        {
            "AAPL": yahoo_quote("AAPL"),
            "XLE": yahoo_quote("XLE", last=95.0, previous_close=94.0),
        }
    )
    lighter = RecordingLighter(
        {
            "AAPL": {"symbol": "AAPL", "market_id": 42, "status": "active",
                     "last_trade_price": 213.5},
            "BTC": {"symbol": "BTC", "market_id": 1, "status": "active",
                    "last_trade_price": 62000.0},
            "SYN": {"symbol": "SYN", "market_id": 6, "status": "active",
                    "last_trade_price": 12.0, "daily_price_change": 1.0},
        }
    )
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    await service._fetch_fresh_quotes(groups)

    # Only listing-venue equities/ETFs are overlay candidates; assets already
    # sourced from Lighter (crypto perps and synthetic equities) are not.
    assert lighter.requested_candidates == {"AAPL", "XLE"}
