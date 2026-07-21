from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.services import daily_board
from app.services.daily_board import DailyBoardService, _sparkline_values


def test_daily_board_builds_regime_breadth_and_theme_ranking(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(
            name="ETF_MACRO",
            assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")],
        ),
        GroupConfig(
            name="TECH",
            assets=[AssetConfig(symbol="NVDA", type="equity", source="yahoo")],
        ),
    ]
    db.save_bars(database, _rising_bars("SPY") + _rising_bars("NVDA"))
    grouped_quotes = {
        "ETF_MACRO": [_quote("SPY", "etf", 126.0, 124.0)],
        "TECH": [_quote("NVDA", "equity", 128.0, 124.0)],
    }

    payload = DailyBoardService(database).build(groups, grouped_quotes)

    assert payload["regime"]["label"] == "RISK-ON / BROAD"  # type: ignore[index]
    assert payload["universe"]["above_200dma_pct"] == 100.0  # type: ignore[index]
    assert payload["universe"]["history_count"] == 2  # type: ignore[index]
    assert payload["themes"][0]["name"] == "TECH"  # type: ignore[index]
    assert len(payload["benchmarks"]) == 1  # type: ignore[arg-type]


def test_market_summaries_include_sparkline_performance_and_range(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(
            name="TECH",
            assets=[AssetConfig(symbol="NVDA", type="equity", source="yahoo")],
        ),
    ]
    db.save_bars(database, _rising_bars("NVDA"))
    grouped_quotes = {"TECH": [_quote("NVDA", "equity", 128.0, 124.0)]}

    summaries = DailyBoardService(database).market_summaries(groups, grouped_quotes)
    summary = summaries["NVDA"]

    assert len(summary["sparkline"]) == 32  # type: ignore[arg-type]
    assert summary["performance"]["1D"] == 3.225806  # type: ignore[index]
    assert summary["performance"]["1W"] is not None  # type: ignore[index]
    assert summary["range_52w"]["current"] == 128.0  # type: ignore[index]
    assert summary["range_52w"]["position_pct"] > 90  # type: ignore[index]


def test_market_summaries_convert_cached_foreign_bars_to_display_currency(
    tmp_path: Path,
) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(
            name="MEMORY",
            assets=[AssetConfig(symbol="000660.KS", type="equity", source="yahoo")],
        ),
    ]
    db.save_bars(
        database,
        [
            *_rising_bars("000660.KS", start_price=2_400_000.0),
            Bar(
                symbol="000660.KS",
                provider="yahoo",
                interval="1d",
                timestamp=datetime(2025, 8, 1, tzinfo=UTC),
                open=1500.0,
                high=1700.0,
                low=1450.0,
                close=1650.0,
            ),
        ],
    )
    grouped_quotes = {
        "MEMORY": [
            Quote.from_last_and_prev_close(
                symbol="000660.KS",
                asset_type="equity",
                provider="yahoo",
                last=2_560_000.0,
                previous_close=2_650_000.0,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                currency="KRW",
                display_last=1_600.0,
                display_previous_close=1_700.0,
                display_change_abs=-100.0,
                display_change_pct=-5.882353,
                display_currency="USD",
            )
        ]
    }

    summaries = DailyBoardService(database).market_summaries(groups, grouped_quotes)
    summary = summaries["000660.KS"]

    assert summary["performance"]["1D"] == -5.882353  # type: ignore[index]
    assert summary["range_52w"]["current"] == 1600.0  # type: ignore[index]
    assert 1000 < summary["range_52w"]["high"] < 2000  # type: ignore[index]


def test_build_board_prepares_each_symbol_once_and_reuses_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(
            name="MIXED",
            assets=[
                AssetConfig(symbol="SPY", type="etf", source="yahoo"),
                # No Stooq series exists; reuse the first cached provider
                # without rescanning/converting it for each output builder.
                AssetConfig(symbol="NVDA", type="equity", source="stooq"),
            ],
        )
    ]
    db.save_bars(database, _rising_bars("SPY") + _rising_bars("NVDA"))
    grouped_quotes = {
        "MIXED": [
            _quote("SPY", "etf", 126.0, 124.0),
            _quote("NVDA", "equity", 128.0, 124.0),
        ]
    }
    calls: list[str] = []
    original = daily_board._display_bars

    def counted(quote: Quote | None, bars: list[Bar]) -> list[Bar]:
        calls.append(quote.symbol if quote else "")
        return original(quote, bars)

    monkeypatch.setattr(daily_board, "_display_bars", counted)

    overview, summaries = DailyBoardService(database).build_board(groups, grouped_quotes)

    assert calls == ["SPY", "NVDA"]
    assert overview["universe"]["history_count"] == 2  # type: ignore[index]
    assert summaries["NVDA"]["has_history"] is True
    assert summaries["NVDA"]["performance"]["1W"] is not None  # type: ignore[index]


def test_sparkline_keeps_full_count_when_current_equals_last_close() -> None:
    closes = [float(value) for value in range(1, 41)]

    # Regression: when current == last close nothing is appended, so the
    # eager pre-trim to count-1 used to leave 31 points instead of 32.
    assert len(_sparkline_values(40.0, closes)) == 32
    assert len(_sparkline_values(41.0, closes)) == 32
    assert _sparkline_values(41.0, closes)[-1] == 41.0


def test_ytd_ignores_stale_quote_timestamp_from_prior_year() -> None:
    # Regression: a stale Dec-31-stamped quote must not anchor the YTD year
    # backwards — early-January boards reported the entire prior year's
    # return as "YTD". Anchored to the wall clock, a history with no bar in
    # the current year measures YTD off the prior year's final close.
    stamp = datetime(2025, 12, 31, tzinfo=UTC)
    bars = [
        Bar(
            symbol="SPY",
            provider="yahoo",
            interval="1d",
            timestamp=stamp - timedelta(days=1 - index),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
        )
        for index, close in enumerate([90.0, 100.0])
    ]
    quote = Quote.from_last_and_prev_close(
        symbol="SPY",
        asset_type="etf",
        provider="yahoo",
        last=105.0,
        previous_close=100.0,
        timestamp=stamp,
    )
    asset = AssetConfig(symbol="SPY", type="etf", source="yahoo")

    summary = daily_board._market_summary(asset, quote, bars)

    # Off the Dec 31 close (100), not the first prior-year bar (90).
    assert summary["performance"]["YTD"] == 5.0  # type: ignore[index]


def test_snapshot_save_failure_is_reported_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DailyBoardService(tmp_path / "board.sqlite3")
    service._last_snapshot_write = -1e9

    def fail_save(
        path: Path,
        snapshot_date: str,
        payload: dict[str, object],
    ) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(db, "save_board_snapshot", fail_save)
    overview: dict[str, object] = {
        "as_of": datetime.now(UTC).isoformat(),
        "universe": {"quoted": 1},
    }

    service._maybe_snapshot(overview)

    assert service.snapshot_status() == {
        "last_success_at": None,
        "last_error": "OSError",
    }



def _rising_bars(symbol: str, start_price: float = 100.0) -> list[Bar]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    for index in range(210):
        close = start_price + index * 0.1
        bars.append(
            Bar(
                symbol=symbol,
                provider="yahoo",
                interval="1d",
                timestamp=start + timedelta(days=index),
                open=close - 0.5,
                high=close + 1,
                low=close - 1,
                close=close,
            )
        )
    return bars


def _quote(symbol: str, asset_type: str, last: float, previous_close: float) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol=symbol,
        asset_type=asset_type,  # type: ignore[arg-type]
        provider="yahoo",
        last=last,
        previous_close=previous_close,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
