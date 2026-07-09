from datetime import UTC, datetime, timedelta

import pytest

from app.models import Bar
from app.providers.yahoo import _drop_off_grid_tail

_SESSION_START = datetime(2026, 7, 8, 13, 30, tzinfo=UTC)


def _bar(timestamp: datetime, interval: str = "1h") -> Bar:
    return Bar(
        symbol="SPY",
        provider="yahoo",
        interval=interval,
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
    )


def _grid(count: int, step: timedelta, interval: str = "1h") -> list[Bar]:
    return [_bar(_SESSION_START + step * i, interval) for i in range(count)]


@pytest.mark.parametrize("interval", ["1h", "60m"])
def test_live_print_tail_inside_hourly_grid_is_dropped(interval: str) -> None:
    # Yahoo appends a row at regularMarketTime mid-bar; here 41m25s after the
    # last on-grid hourly bar. Persisting it accretes phantom candles.
    grid = _grid(3, timedelta(hours=1), interval)
    live_print = _bar(grid[-1].timestamp + timedelta(minutes=41, seconds=25), interval)

    assert _drop_off_grid_tail(grid + [live_print], interval) == grid


def test_clean_hourly_grid_is_kept() -> None:
    grid = _grid(4, timedelta(hours=1))

    assert _drop_off_grid_tail(grid, "1h") == grid


@pytest.mark.parametrize(
    ("interval", "step_seconds"),
    [("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800), ("1h", 3600), ("60m", 3600)],
)
def test_tail_exactly_one_interval_after_predecessor_is_kept(
    interval: str, step_seconds: int
) -> None:
    # A genuine partial bar sits ON the grid — exactly one interval after its
    # predecessor — and must survive for every intraday interval.
    bars = [
        _bar(_SESSION_START, interval),
        _bar(_SESSION_START + timedelta(seconds=step_seconds), interval),
    ]

    assert _drop_off_grid_tail(bars, interval) == bars


def test_weekend_gap_larger_than_interval_is_kept() -> None:
    friday = _grid(2, timedelta(hours=1))
    monday = _bar(friday[-1].timestamp + timedelta(days=2))

    assert _drop_off_grid_tail(friday + [monday], "1h") == friday + [monday]


def test_single_bar_and_empty_lists_pass_through() -> None:
    lone = [_bar(_SESSION_START)]

    assert _drop_off_grid_tail(lone, "1h") == lone
    assert _drop_off_grid_tail([], "1h") == []


@pytest.mark.parametrize("interval", ["1d", "1wk", "1mo"])
def test_non_intraday_intervals_never_drop(interval: str) -> None:
    # Daily-and-up responses have no live-print tail convention; even a
    # 61-second gap must pass through untouched.
    bars = [
        _bar(_SESSION_START, interval),
        _bar(_SESSION_START + timedelta(seconds=61), interval),
    ]

    assert _drop_off_grid_tail(bars, interval) == bars
