from datetime import UTC, datetime, timedelta, timezone

from app.models import Bar, Quote
from app.services.daily_board import _relative_volume

_TODAY = datetime(2026, 7, 3, 15, 30, tzinfo=UTC)


def _bar(timestamp: datetime, volume: float | None) -> Bar:
    return Bar(
        symbol="SPY",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=volume,
    )


def _completed_bars(count: int, volume: float = 100.0) -> list[Bar]:
    """`count` ascending daily bars, the newest dated the day before _TODAY."""
    return [_bar(_TODAY - timedelta(days=count - i), volume) for i in range(count)]


def _quote(volume: float | None, timestamp: datetime = _TODAY) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol="SPY",
        asset_type="etf",
        provider="yahoo",
        last=100.0,
        previous_close=99.0,
        timestamp=timestamp,
        volume=volume,
    )


def test_partial_today_bar_excluded_and_quote_volume_preferred() -> None:
    # The huge in-progress bar must be dropped from the baseline AND must not
    # be used as the numerator while quote volume is available.
    bars = _completed_bars(20) + [_bar(_TODAY.replace(hour=14), 1_000_000.0)]

    assert _relative_volume(_quote(volume=250.0), bars) == 2.5


def test_falls_back_to_partial_bar_volume_when_quote_volume_missing() -> None:
    bars = _completed_bars(20) + [_bar(_TODAY.replace(hour=14), 50.0)]

    assert _relative_volume(_quote(volume=None), bars) == 0.5


def test_stale_last_bar_counts_toward_baseline() -> None:
    # Newest bar is dated yesterday: it is a completed session, so exactly 10
    # bars satisfies the minimum-baseline requirement.
    bars = _completed_bars(10)

    assert _relative_volume(_quote(volume=300.0), bars) == 3.0


def test_no_current_volume_when_last_bar_is_not_today_returns_none() -> None:
    bars = _completed_bars(15)

    assert _relative_volume(_quote(volume=None), bars) is None


def test_none_quote_with_historical_bars_returns_none() -> None:
    bars = [
        _bar(datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i), 100.0) for i in range(15)
    ]

    assert _relative_volume(None, bars) is None


def test_fewer_than_ten_baseline_bars_returns_none() -> None:
    bars = _completed_bars(9) + [_bar(_TODAY.replace(hour=14), 500.0)]

    assert _relative_volume(_quote(volume=500.0), bars) is None


def test_zero_volume_bars_do_not_count_toward_baseline() -> None:
    # 12 completed sessions but only 9 with volume: below the 10-bar minimum.
    bars = [
        _bar(_TODAY - timedelta(days=12 - i), 100.0 if i < 9 else 0.0) for i in range(12)
    ]

    assert _relative_volume(_quote(volume=250.0), bars) is None


def test_non_positive_baseline_average_returns_none() -> None:
    bars = [_bar(_TODAY - timedelta(days=10 - i), -100.0) for i in range(10)]

    assert _relative_volume(_quote(volume=250.0), bars) is None


def test_no_bars_returns_none() -> None:
    assert _relative_volume(_quote(volume=250.0), []) is None


def test_result_rounds_to_two_decimals() -> None:
    bars = _completed_bars(20, volume=300.0)

    assert _relative_volume(_quote(volume=100.0), bars) == 0.33


def test_bar_dates_are_compared_in_utc() -> None:
    # 2026-07-03 04:00 Tokyo time is 2026-07-02 19:00 UTC — the same UTC date
    # as the quote, so the bar is partial: excluded from the baseline and used
    # as the volume fallback.
    tokyo = timezone(timedelta(hours=9))
    quote_ts = datetime(2026, 7, 2, 20, 0, tzinfo=UTC)
    completed = [_bar(quote_ts - timedelta(days=21 - i), 100.0) for i in range(20)]
    bars = completed + [_bar(datetime(2026, 7, 3, 4, 0, tzinfo=tokyo), 50.0)]

    assert _relative_volume(_quote(volume=None, timestamp=quote_ts), bars) == 0.5
