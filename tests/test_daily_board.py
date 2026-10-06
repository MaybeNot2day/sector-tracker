from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import db
from app.calendar_windows import months_before
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


def test_auto_hyperliquid_groups_stay_out_of_themes_and_universe(tmp_path: Path) -> None:
    """Discovery groups feed the Markets grid only: no own theme, no breadth."""
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(
            name="TECH",
            assets=[AssetConfig(symbol="NVDA", type="equity", source="yahoo")],
        ),
        GroupConfig(
            name="HYPERLIQUID_NEW_CRYPTO",
            assets=[AssetConfig(symbol="NEWCOIN", type="crypto_perp", source="hyperliquid")],
        ),
        GroupConfig(
            name="HYPERLIQUID_NEW_XYZ",
            assets=[AssetConfig(symbol="NEWSTOCK", type="equity", source="hyperliquid")],
        ),
    ]
    db.save_bars(database, _rising_bars("NVDA"))
    grouped_quotes = {
        "TECH": [_quote("NVDA", "equity", 128.0, 124.0)],
        "HYPERLIQUID_NEW_CRYPTO": [_quote("NEWCOIN", "crypto_perp", 2.0, 1.0)],
        "HYPERLIQUID_NEW_XYZ": [_quote("NEWSTOCK", "equity", 3.0, 4.0)],
    }

    overview, summaries = DailyBoardService(database).build_board(groups, grouped_quotes)

    theme_names = [theme["name"] for theme in overview["themes"]]  # type: ignore[index]
    assert theme_names == ["TECH"]
    assert overview["universe"]["total"] == 1  # type: ignore[index]
    assert overview["universe"]["quoted"] == 1  # type: ignore[index]
    rotation = overview["rotation"]
    assert all(
        theme["name"] == "TECH"
        for bucket in rotation.values()  # type: ignore[union-attr]
        for theme in bucket
    )
    # The listings themselves still reach the Markets grid.
    assert "NEWCOIN" in summaries and "NEWSTOCK" in summaries


def test_build_board_reuses_bars_until_the_table_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bars table changes at most hourly, but the board rebuilds every
    quote poll: the full-table load must be cached on db.bars_revision."""
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(name="TECH", assets=[AssetConfig(symbol="NVDA", type="equity", source="yahoo")])
    ]
    db.save_bars(database, _rising_bars("NVDA"))
    grouped_quotes = {"TECH": [_quote("NVDA", "equity", 128.0, 124.0)]}
    service = DailyBoardService(database)
    loads = 0
    original_load = db.load_bars_by_symbol

    def counting_load(*args: object, **kwargs: object) -> object:
        nonlocal loads
        loads += 1
        return original_load(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db, "load_bars_by_symbol", counting_load)

    first = service.build(groups, grouped_quotes)
    second = service.build(groups, grouped_quotes)
    assert loads == 1
    assert first["universe"] == second["universe"]

    # A bar write bumps the revision and invalidates the cached load.
    db.save_bars(
        database,
        [
            Bar(
                symbol="NVDA",
                provider="yahoo",
                interval="1d",
                timestamp=datetime.now(UTC),
                open=130.0,
                high=131.0,
                low=129.0,
                close=130.5,
            )
        ],
    )
    service.build(groups, grouped_quotes)
    assert loads == 2


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


def test_first_snapshot_writes_when_monotonic_clock_is_near_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_dates: list[str] = []
    service = DailyBoardService(tmp_path / "board.sqlite3")
    monkeypatch.setattr(daily_board, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        db,
        "save_board_snapshot",
        lambda path, snapshot_date, payload: saved_dates.append(snapshot_date),
    )

    service._maybe_snapshot(
        {
            "as_of": datetime.now(UTC).isoformat(),
            "universe": {"quoted": 1},
        }
    )

    assert saved_dates == [datetime.now(UTC).date().isoformat()]


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


def test_board_observes_same_timestamp_correction_and_delete(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    stamp = datetime.now(UTC)
    asset = AssetConfig("SPY", "etf", "yahoo")
    groups = [GroupConfig("TEST", [asset])]
    db.save_bars(
        database, [Bar("SPY", "yahoo", "1d", stamp, 100.0, 110.0, 90.0, 100.0)]
    )
    service = DailyBoardService(database)
    assert service.market_summaries(groups, {})["SPY"]["range_52w"]["current"] == 100.0  # type: ignore[index]
    db.save_bars(
        database, [Bar("SPY", "yahoo", "1d", stamp, 100.0, 110.0, 90.0, 105.0)]
    )
    assert service.market_summaries(groups, {})["SPY"]["range_52w"]["current"] == 105.0  # type: ignore[index]
    with db._connect(database) as conn:
        conn.execute("DELETE FROM bars WHERE symbol = 'SPY'")
    summary = service.market_summaries(groups, {})["SPY"]
    assert summary["has_history"] is False
    assert summary["range_52w"] is None


@pytest.mark.parametrize(
    ("asset_type", "provider"),
    [("crypto_perp", "hyperliquid"), ("crypto_spot", "yahoo"), ("equity", "hyperliquid")],
)
def test_continuous_daily_metrics_use_calendar_windows(
    asset_type: str, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import cast

    from app.models import AssetType, ProviderName

    now = datetime(2026, 7, 31, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return now

    monkeypatch.setattr(daily_board, "datetime", Clock)
    start = datetime(2025, 7, 1, tzinfo=UTC)
    references = {
        "2025-07-31": 60.0,  # true one-calendar-year anchor
        "2026-07-24": 100.0,
        "2026-06-30": 80.0,  # month-end clamp, not 22 sessions / 31 days
        "2026-04-30": 75.0,
    }
    bars = []
    for day in range((now.date() - start.date()).days + 1):
        stamp = start + timedelta(days=day)
        close = references.get(stamp.date().isoformat(), 110.0)
        high, low = close + 1, close - 1
        if stamp.date().isoformat() == "2025-07-30":
            high, low = 200.0, 20.0  # outside the rolling 52-week range
        if stamp.date().isoformat() == "2025-09-01":
            high, low = 180.0, 40.0  # inside 52 weeks, outside 252 crypto bars
        bars.append(
            Bar("BTC", cast(ProviderName, provider), "1d", stamp, close, high, low, close)
        )
    asset = AssetConfig("BTC", cast(AssetType, asset_type), cast(ProviderName, provider))
    quote = Quote.from_last_and_prev_close(
        symbol="BTC", asset_type=asset.type, provider=asset.source,
        last=120.0, previous_close=110.0, timestamp=now,
    )
    summary = daily_board._market_summary(asset, quote, bars)
    performance = summary["performance"]
    assert performance["1W"] == 20.0  # type: ignore[index]
    assert performance["1M"] == 50.0  # type: ignore[index]
    assert performance["3M"] == 60.0  # type: ignore[index]
    assert performance["1Y"] == 100.0  # type: ignore[index]
    assert summary["range_52w"]["high"] == 180.0  # type: ignore[index]
    assert summary["range_52w"]["low"] == 40.0  # type: ignore[index]
    assert daily_board._market_summary(asset, quote, bars[-300:])["performance"]["1Y"] is None  # type: ignore[index]
    high_quote = Quote.from_last_and_prev_close(
        symbol="BTC", asset_type=asset.type, provider=asset.source,
        last=150.0, previous_close=110.0, timestamp=now,
    )
    assert daily_board._asset_metrics(asset, high_quote, bars)["high_52w"] is False
    low_quote = Quote.from_last_and_prev_close(
        symbol="BTC", asset_type=asset.type, provider=asset.source,
        last=50.0, previous_close=110.0, timestamp=now,
    )
    assert daily_board._asset_metrics(asset, low_quote, bars)["low_52w"] is False


def test_board_load_keeps_a_true_year_of_continuous_daily_history(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    anchor = months_before(today, 12)
    start = anchor - timedelta(days=7)
    bars = [
        Bar(
            "BTC", "hyperliquid", "1d", start + timedelta(days=day),
            100.0, 121.0, 99.0, 100.0,
        )
        for day in range((today - start).days + 1)
    ]
    db.save_bars(database, bars)
    asset = AssetConfig("BTC", "crypto_perp", "hyperliquid")
    quote = Quote.from_last_and_prev_close(
        symbol="BTC", asset_type=asset.type, provider=asset.source,
        last=120.0, previous_close=100.0, timestamp=datetime.now(UTC),
    )
    summary = DailyBoardService(database).market_summaries(
        [GroupConfig("CRYPTO", [asset])], {"CRYPTO": [quote]}
    )["BTC"]
    assert summary["performance"]["1Y"] == 20.0  # type: ignore[index]


def test_official_equity_history_retains_session_windows_with_hyperliquid_quote() -> None:
    asset = AssetConfig("SPY", "etf", "yahoo")
    now = datetime.now(UTC)
    bars = []
    stamp = now - timedelta(days=500)
    while len(bars) < 260:
        if stamp.weekday() < 5:
            close = 100.0
            bars.append(Bar("SPY", "yahoo", "1d", stamp, close, 150.0, 50.0, close))
        stamp += timedelta(days=1)
    for offset, close in ((6, 100.0), (22, 80.0), (64, 75.0), (252, 60.0)):
        bar = bars[-offset]
        bars[-offset] = Bar("SPY", "yahoo", "1d", bar.timestamp, close, 150.0, 50.0, close)
    quote = Quote.from_last_and_prev_close(
        symbol="SPY", asset_type="etf", provider="hyperliquid",
        last=120.0, previous_close=100.0, timestamp=now,
    )
    summary = daily_board._market_summary(asset, quote, bars)
    assert summary["performance"]["1W"] == 20.0  # type: ignore[index]
    assert summary["performance"]["1M"] == 50.0  # type: ignore[index]
    assert summary["performance"]["3M"] == 60.0  # type: ignore[index]
    assert summary["performance"]["1Y"] == 100.0  # type: ignore[index]
