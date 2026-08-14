from datetime import UTC, datetime

from app.models import Bar
from app.providers.aggregate import aggregate_bars


def hourly(
    timestamp: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float | None = None,
) -> Bar:
    return Bar(
        symbol="BTC",
        provider="hyperliquid",
        interval="1h",
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def daily(
    timestamp: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float | None = None,
) -> Bar:
    return Bar(
        symbol="BTC",
        provider="hyperliquid",
        interval="1d",
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def test_4h_buckets_align_to_utc_windows_and_merge_ohlcv() -> None:
    # 00:30 and 03:59 fall in the same UTC-aligned 4h window; 04:00 opens
    # the next one.
    day = datetime(2026, 6, 15, tzinfo=UTC)
    bars = [
        hourly(day.replace(hour=0, minute=30), 100.0, 110.0, 95.0, 105.0, 10.0),
        hourly(day.replace(hour=1, minute=30), 105.0, 120.0, 104.0, 118.0, 7.0),
        hourly(day.replace(hour=3, minute=59), 118.0, 119.0, 90.0, 93.0, 3.0),
        hourly(day.replace(hour=4), 93.0, 94.0, 92.0, 94.0, 5.0),
    ]

    assert aggregate_bars(bars, "4h") == [
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="4h",
            timestamp=day.replace(hour=0, minute=30),
            open=100.0,
            high=120.0,
            low=90.0,
            close=93.0,
            volume=20.0,
        ),
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="4h",
            timestamp=day.replace(hour=4),
            open=93.0,
            high=94.0,
            low=92.0,
            close=94.0,
            volume=5.0,
        ),
    ]


def test_weekly_buckets_split_on_iso_week_including_year_rollover() -> None:
    # Fri 2024-12-27 and Sun 2024-12-29 are ISO 2024-W52; Mon 2024-12-30 and
    # Thu 2025-01-02 are ISO 2025-W01 (one bucket despite the calendar-year
    # change); Mon 2025-01-06 starts 2025-W02.
    bars = [
        daily(datetime(2024, 12, 27, tzinfo=UTC), 10.0, 12.0, 9.0, 11.0, 100.0),
        daily(datetime(2024, 12, 29, tzinfo=UTC), 11.0, 15.0, 8.0, 14.0, 50.0),
        daily(datetime(2024, 12, 30, tzinfo=UTC), 14.0, 16.0, 13.0, 15.0, 40.0),
        daily(datetime(2025, 1, 2, tzinfo=UTC), 15.0, 15.5, 12.0, 12.5, 60.0),
        daily(datetime(2025, 1, 6, tzinfo=UTC), 12.5, 13.0, 12.0, 12.75, 30.0),
    ]

    assert aggregate_bars(bars, "1wk") == [
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="1wk",
            timestamp=datetime(2024, 12, 27, tzinfo=UTC),
            open=10.0,
            high=15.0,
            low=8.0,
            close=14.0,
            volume=150.0,
        ),
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="1wk",
            timestamp=datetime(2024, 12, 30, tzinfo=UTC),
            open=14.0,
            high=16.0,
            low=12.0,
            close=12.5,
            volume=100.0,
        ),
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="1wk",
            timestamp=datetime(2025, 1, 6, tzinfo=UTC),
            open=12.5,
            high=13.0,
            low=12.0,
            close=12.75,
            volume=30.0,
        ),
    ]


def test_monthly_buckets_split_on_calendar_month() -> None:
    bars = [
        daily(datetime(2026, 1, 30, tzinfo=UTC), 5.0, 6.0, 4.0, 5.5, 10.0),
        daily(datetime(2026, 1, 31, tzinfo=UTC), 5.5, 7.0, 5.0, 6.5, 20.0),
        daily(datetime(2026, 2, 1, tzinfo=UTC), 6.5, 6.6, 6.0, 6.2, 30.0),
    ]

    assert aggregate_bars(bars, "1mo") == [
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="1mo",
            timestamp=datetime(2026, 1, 30, tzinfo=UTC),
            open=5.0,
            high=7.0,
            low=4.0,
            close=6.5,
            volume=30.0,
        ),
        Bar(
            symbol="BTC",
            provider="hyperliquid",
            interval="1mo",
            timestamp=datetime(2026, 2, 1, tzinfo=UTC),
            open=6.5,
            high=6.6,
            low=6.0,
            close=6.2,
            volume=30.0,
        ),
    ]


def test_bucket_volume_stays_none_only_when_all_members_lack_volume() -> None:
    # Both days sit in ISO week 2026-W25.
    all_none = [
        daily(datetime(2026, 6, 15, tzinfo=UTC), 1.0, 2.0, 0.5, 1.5, None),
        daily(datetime(2026, 6, 16, tzinfo=UTC), 1.5, 1.8, 1.2, 1.6, None),
    ]
    assert aggregate_bars(all_none, "1wk")[0].volume is None

    mixed = [
        daily(datetime(2026, 6, 15, tzinfo=UTC), 1.0, 2.0, 0.5, 1.5, None),
        daily(datetime(2026, 6, 16, tzinfo=UTC), 1.5, 1.8, 1.2, 1.6, 4.0),
        daily(datetime(2026, 6, 17, tzinfo=UTC), 1.6, 1.7, 1.4, 1.5, None),
    ]
    assert aggregate_bars(mixed, "1wk")[0].volume == 4.0


def test_unknown_interval_and_empty_input_pass_through_unchanged() -> None:
    bars = [
        daily(datetime(2026, 6, 15, tzinfo=UTC), 1.0, 2.0, 0.5, 1.5, 10.0),
        daily(datetime(2026, 6, 16, tzinfo=UTC), 1.5, 1.8, 1.2, 1.6, 20.0),
    ]

    assert aggregate_bars(bars, "1d") == bars
    assert aggregate_bars([], "4h") == []
