from datetime import UTC, datetime
from pathlib import Path

from app import db
from app.models import Bar, Quote


def test_save_and_load_latest_quote(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    quote = Quote.from_last_and_prev_close(
        symbol="SPY",
        asset_type="etf",
        provider="yahoo",
        last=510.0,
        previous_close=500.0,
        timestamp=datetime.now(UTC),
        currency="USD",
        display_last=510.0,
        display_previous_close=500.0,
        display_change_abs=10.0,
        display_change_pct=2.0,
        display_currency="USD",
    )

    db.save_quotes(database, [quote])
    loaded = db.load_latest_quote(database, "spy")

    assert loaded is not None
    assert loaded.symbol == "SPY"
    assert loaded.change_pct == 2.0
    assert loaded.currency == "USD"
    assert loaded.display_last == 510.0
    assert loaded.display_currency == "USD"


def test_quote_round_trip_persists_volume_and_derivative_fields(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    timestamp = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    quote = Quote.from_last_and_prev_close(
        symbol="BTC",
        asset_type="crypto_perp",
        provider="lighter",
        last=65_000.0,
        previous_close=64_000.0,
        timestamp=timestamp,
        volume=123_456.5,
        funding_rate=1.25e-05,
        open_interest_usd=2_500_000_000.0,
    )

    db.save_quotes(database, [quote])
    loaded = db.load_latest_quote(database, "BTC")

    assert loaded is not None
    assert loaded.volume == 123_456.5
    assert loaded.funding_rate == 1.25e-05
    assert loaded.open_interest_usd == 2_500_000_000.0

    # The upsert must overwrite the new columns when a refresh lacks them.
    refreshed = Quote.from_last_and_prev_close(
        symbol="BTC",
        asset_type="crypto_perp",
        provider="lighter",
        last=65_500.0,
        previous_close=64_000.0,
        timestamp=timestamp,
    )
    db.save_quotes(database, [refreshed])
    reloaded = db.load_latest_quote(database, "BTC")

    assert reloaded is not None
    assert reloaded.volume is None
    assert reloaded.funding_rate is None
    assert reloaded.open_interest_usd is None


def test_save_and_load_bars(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    bar = Bar(
        symbol="NVDA",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=100.0,
        high=110.0,
        low=95.0,
        close=108.0,
        volume=1_000_000.0,
    )

    db.save_bars(database, [bar])
    loaded = db.load_bars(database, "NVDA", "1d", "yahoo")

    assert loaded == [bar]
