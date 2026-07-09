from datetime import UTC, datetime

from app.models import Bar
from app.services.daily_board import _ytd_return


def _bar(timestamp: datetime, close: float) -> Bar:
    return Bar(
        symbol="SPY",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=close,
        high=close,
        low=close,
        close=close,
    )


def test_early_january_with_only_prior_year_bars_anchors_to_final_close() -> None:
    # The regression: on Jan 2 the cache still ends on Dec 31, and anchoring
    # to the newest bar's year reported the ENTIRE prior-year rally
    # (80 -> 100, +25.125%) as "YTD" instead of the move off the final close.
    bars = [
        _bar(datetime(2025, 1, 2, tzinfo=UTC), 80.0),
        _bar(datetime(2025, 6, 30, tzinfo=UTC), 92.0),
        _bar(datetime(2025, 12, 31, tzinfo=UTC), 100.0),
    ]

    assert _ytd_return(100.1, bars, datetime(2026, 1, 2, 14, 30, tzinfo=UTC)) == 0.1


def test_mid_year_uses_last_prior_year_close_as_reference() -> None:
    bars = [
        _bar(datetime(2025, 12, 30, tzinfo=UTC), 95.0),
        _bar(datetime(2025, 12, 31, tzinfo=UTC), 100.0),
        _bar(datetime(2026, 1, 2, tzinfo=UTC), 101.0),
        _bar(datetime(2026, 7, 8, tzinfo=UTC), 109.0),
    ]

    # Reference is the Dec 31 close (100), not the first bar of the year (101).
    assert _ytd_return(110.0, bars, datetime(2026, 7, 9, tzinfo=UTC)) == 10.0


def test_none_as_of_defaults_to_current_utc_year() -> None:
    # All bars sit in the previous calendar year, so with the default clock
    # the anchor is the newest close — whenever this test runs.
    year = datetime.now(UTC).year
    bars = [
        _bar(datetime(year - 1, 1, 15, tzinfo=UTC), 80.0),
        _bar(datetime(year - 1, 12, 31, tzinfo=UTC), 100.0),
    ]

    assert _ytd_return(100.1, bars) == 0.1


def test_prior_year_as_of_computes_that_years_ytd() -> None:
    # A stale quote from last year anchors YTD to ITS year, not to today's.
    bars = [
        _bar(datetime(2024, 12, 31, tzinfo=UTC), 100.0),
        _bar(datetime(2025, 1, 2, tzinfo=UTC), 102.0),
        _bar(datetime(2025, 6, 30, tzinfo=UTC), 108.0),
    ]

    assert _ytd_return(110.0, bars, datetime(2025, 7, 1, tzinfo=UTC)) == 10.0


def test_history_starting_in_anchor_year_uses_first_close() -> None:
    # New listing: no prior-year close exists, YTD runs from the first bar.
    bars = [
        _bar(datetime(2026, 3, 2, tzinfo=UTC), 50.0),
        _bar(datetime(2026, 7, 8, tzinfo=UTC), 60.0),
    ]

    assert _ytd_return(62.0, bars, datetime(2026, 7, 9, tzinfo=UTC)) == 24.0


def test_missing_inputs_or_zero_reference_return_none() -> None:
    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    bars = [_bar(datetime(2025, 12, 31, tzinfo=UTC), 100.0)]

    assert _ytd_return(None, bars, as_of) is None
    assert _ytd_return(100.0, [], as_of) is None
    assert _ytd_return(100.0, [_bar(datetime(2025, 12, 31, tzinfo=UTC), 0.0)], as_of) is None
