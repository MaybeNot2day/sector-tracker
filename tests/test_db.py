import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import cast

import pytest

from app import db
from app.models import Bar, ProviderName, Quote


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


def test_save_bars_quarantines_all_invalid_batch(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    invalid_bar = Bar(
        symbol="NVDA",
        provider="yahoo",
        interval="1d",
        timestamp=datetime(2026, 1, 2, tzinfo=UTC),
        open=100.0,
        high=105.0,
        low=95.0,
        close=108.0,
        volume=1_000_000.0,
    )

    db.save_bars(database, [invalid_bar])

    assert db.load_bars(database, "NVDA", "1d", "yahoo") == []
    with db._connect(database) as conn:
        quarantined = conn.execute(
            """
            SELECT symbol, provider, interval, timestamp, open, high, low, close, volume, reason
            FROM invalid_bars
            """
        ).fetchone()
    assert quarantined is not None
    assert tuple(quarantined) == (
        "NVDA",
        "yahoo",
        "1d",
        "2026-01-02T00:00:00+00:00",
        100.0,
        105.0,
        95.0,
        108.0,
        1_000_000.0,
        "invalid_ohlc",
    )


def test_init_db_runs_schema_once_per_path_and_reinitializes_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "board.sqlite3"
    original_ensure_column = db._ensure_column
    probe_count = 0
    count_lock = Lock()

    def counted_ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        nonlocal probe_count
        with count_lock:
            probe_count += 1
        original_ensure_column(conn, table, column, definition)

    monkeypatch.setattr(db, "_ensure_column", counted_ensure_column)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(db.init_db, [database] * 16))

    assert probe_count == 10
    assert db.load_latest_quote(database, "MISSING") is None
    assert probe_count == 10

    database.unlink()
    db.init_db(database)

    assert database.exists()
    assert probe_count == 20


def test_load_latest_quotes_batches_normalizes_and_deduplicates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "board.sqlite3"
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    quotes = [
        Quote.from_last_and_prev_close(
            symbol=symbol,
            asset_type="equity",
            provider="yahoo",
            last=last,
            previous_close=last - 1,
            timestamp=timestamp,
        )
        for symbol, last in (("AAPL", 200.0), ("XME", 70.0))
    ]
    db.save_quotes(database, quotes)

    loaded = db.load_latest_quotes(database, ["xme", "AAPL", "aapl", "missing"])

    assert list(loaded) == ["AAPL", "XME"]
    assert loaded["AAPL"] == quotes[0]
    assert loaded["XME"] == quotes[1]
    assert db.load_latest_quotes(database, []) == {}


def test_load_bars_by_symbol_limits_each_partition_in_sql(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(
            symbol=symbol,
            provider=cast(ProviderName, provider),
            interval="1d",
            timestamp=start + timedelta(days=day),
            open=float(day),
            high=float(day + 2),
            low=float(day),
            close=float(day + 1),
            volume=None,
        )
        for symbol, provider, count in (
            ("AAPL", "yahoo", 7),
            ("AAPL", "stooq", 5),
            ("BTC", "lighter", 6),
        )
        for day in range(count)
    ]
    db.save_bars(database, bars)
    with db._connect(database) as conn:
        index_columns = [
            str(row["name"])
            for row in conn.execute(
                "PRAGMA index_info(idx_bars_interval_symbol_provider_timestamp)"
            )
        ]
    assert index_columns == ["interval", "symbol", "provider", "timestamp"]


    loaded = db.load_bars_by_symbol(database, "1d", limit_per_series=3)

    assert set(loaded) == {
        ("AAPL", "yahoo"),
        ("AAPL", "stooq"),
        ("BTC", "lighter"),
    }
    assert {
        key: [bar.timestamp for bar in series] for key, series in loaded.items()
    } == {
        ("AAPL", "yahoo"): [start + timedelta(days=day) for day in (4, 5, 6)],
        ("AAPL", "stooq"): [start + timedelta(days=day) for day in (2, 3, 4)],
        ("BTC", "lighter"): [start + timedelta(days=day) for day in (3, 4, 5)],
    }
    assert all(len(series) == 3 for series in loaded.values())

    with pytest.raises(ValueError, match="must be positive"):
        db.load_bars_by_symbol(database, "1d", limit_per_series=0)
