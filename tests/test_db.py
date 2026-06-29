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
    )

    db.save_quotes(database, [quote])
    loaded = db.load_latest_quote(database, "spy")

    assert loaded is not None
    assert loaded.symbol == "SPY"
    assert loaded.change_pct == 2.0


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

