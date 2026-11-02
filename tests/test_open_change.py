from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.models import AssetConfig, Bar, Quote
from app.providers.yahoo import _session_open
from app.services.daily_board import _market_summary, _open_change_pct, _today_bar_open

_TODAY = datetime(2026, 7, 3, 15, 30, tzinfo=UTC)
_YESTERDAY = _TODAY - timedelta(days=1)


def _bar(timestamp: datetime, open_: float = 100.0, close: float = 100.5) -> Bar:
    return Bar(
        symbol="SPY",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=open_,
        high=max(open_, close) + 1.0,
        low=min(open_, close) - 1.0,
        close=close,
    )


def _quote(
    last: float = 100.0,
    *,
    timestamp: datetime = _TODAY,
    currency: str | None = None,
    open_price: float | None = None,
    display_last: float | None = None,
) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol="SPY",
        asset_type="etf",
        provider="yahoo",
        last=last,
        previous_close=99.0,
        timestamp=timestamp,
        currency=currency,
        open_price=open_price,
        display_last=display_last,
    )


# --- _open_change_pct -------------------------------------------------------


def test_change_from_todays_bar_open() -> None:
    assert _open_change_pct(_quote(103.5), [_bar(_TODAY, open_=100.0)]) == 3.5


def test_negative_change_from_open() -> None:
    assert _open_change_pct(_quote(95.0), [_bar(_TODAY, open_=100.0)]) == -5.0


def test_result_rounds_to_four_decimals() -> None:
    assert _open_change_pct(_quote(100.123456), [_bar(_TODAY, open_=100.0)]) == 0.1235


def test_display_last_preferred_over_raw_last() -> None:
    # An FX-normalized quote must measure the move in display terms; using the
    # raw last (15 000) against the bar open would report +14 900 %.
    quote = _quote(15_000.0, display_last=103.5)

    assert _open_change_pct(quote, [_bar(_TODAY, open_=100.0)]) == 3.5


def test_todays_bar_open_preferred_over_quote_open_price() -> None:
    quote = _quote(103.5, open_price=50.0)

    assert _open_change_pct(quote, [_bar(_TODAY, open_=100.0)]) == 3.5


def test_stale_bar_falls_back_to_quote_open_without_currency() -> None:
    quote = _quote(103.0, open_price=100.0, currency=None)

    assert _open_change_pct(quote, [_bar(_YESTERDAY, open_=50.0)]) == 3.0


def test_stale_bar_falls_back_to_quote_open_for_usd() -> None:
    quote = _quote(103.0, open_price=100.0, currency="USD")

    assert _open_change_pct(quote, [_bar(_YESTERDAY, open_=50.0)]) == 3.0


def test_non_usd_quote_open_is_never_used() -> None:
    # Non-USD quote opens are in local currency while display is bar-based;
    # mixing them would fabricate a huge from-open move.
    quote = _quote(103.0, open_price=100.0, currency="EUR")

    assert _open_change_pct(quote, [_bar(_YESTERDAY, open_=50.0)]) is None


def test_zero_bar_open_falls_back_to_quote_open() -> None:
    quote = _quote(102.0, open_price=100.0)

    assert _open_change_pct(quote, [_bar(_TODAY, open_=0.0)]) == 2.0


def test_zero_bar_open_without_fallback_returns_none() -> None:
    assert _open_change_pct(_quote(102.0), [_bar(_TODAY, open_=0.0)]) is None


@pytest.mark.parametrize("open_price", [0.0, -10.0], ids=["zero", "negative"])
def test_non_positive_quote_open_returns_none(open_price: float) -> None:
    assert _open_change_pct(_quote(103.0, open_price=open_price), []) is None


def test_current_price_falls_back_to_last_bar_close() -> None:
    # Quote carries no positive price, so the bar close is the current price.
    bars = [_bar(_TODAY, open_=100.0, close=104.0)]

    assert _open_change_pct(_quote(0.0), bars) == 4.0


def test_zero_current_price_returns_none() -> None:
    bars = [_bar(_TODAY, open_=100.0, close=0.0)]

    assert _open_change_pct(_quote(0.0), bars) is None


def test_no_current_price_returns_none() -> None:
    assert _open_change_pct(_quote(0.0), []) is None
    assert _open_change_pct(None, []) is None


def test_no_open_available_returns_none() -> None:
    assert _open_change_pct(_quote(103.0), []) is None
    assert _open_change_pct(_quote(103.0), [_bar(_YESTERDAY)]) is None


# --- _today_bar_open --------------------------------------------------------


def test_today_bar_open_returned() -> None:
    bars = [_bar(_TODAY.replace(hour=0), open_=101.25)]

    assert _today_bar_open(_quote(), bars) == 101.25


def test_empty_bars_return_none() -> None:
    assert _today_bar_open(_quote(), []) is None


def test_previous_day_bar_returns_none() -> None:
    assert _today_bar_open(_quote(), [_bar(_YESTERDAY)]) is None


def test_only_the_last_bar_is_considered() -> None:
    # A today-dated bar hidden behind a stale final bar must not be used.
    bars = [_bar(_TODAY, open_=101.0), _bar(_YESTERDAY, open_=102.0)]

    assert _today_bar_open(_quote(), bars) is None


@pytest.mark.parametrize("open_", [0.0, -1.0], ids=["zero", "negative"])
def test_non_positive_bar_open_returns_none(open_: float) -> None:
    assert _today_bar_open(_quote(), [_bar(_TODAY, open_=open_)]) is None


def test_bar_date_compared_in_utc() -> None:
    # 2026-07-04 04:00 Tokyo time is 2026-07-03 19:00 UTC — the same UTC date
    # as the quote despite the different local date.
    tokyo = timezone(timedelta(hours=9))
    bars = [_bar(datetime(2026, 7, 4, 4, 0, tzinfo=tokyo), open_=101.5)]

    assert _today_bar_open(_quote(), bars) == 101.5


def test_naive_quote_timestamp_treated_as_utc() -> None:
    # 23:30 naive is still 2026-07-03 when read as UTC; a local-time reading
    # on a machine west of UTC would roll it into July 4 and drop the bar.
    quote = _quote(timestamp=datetime(2026, 7, 3, 23, 30))

    assert _today_bar_open(quote, [_bar(_TODAY, open_=101.5)]) == 101.5


def test_none_quote_compares_bar_against_now() -> None:
    bars = [_bar(datetime.now(UTC), open_=42.0)]

    assert _today_bar_open(None, bars) == 42.0


# --- yahoo._session_open ----------------------------------------------------


def _chart_result(quote_indicators: dict[str, Any]) -> dict[str, Any]:
    return {"indicators": {"quote": [quote_indicators]}}


@pytest.mark.parametrize(
    ("indicators", "expected"),
    [
        pytest.param({"open": [None, 0, 101.5, 102.0]}, 101.5, id="skips-leading-null-and-zero"),
        pytest.param({"open": [-3.0, 101.5]}, 101.5, id="skips-negative"),
        pytest.param({"open": [float("nan"), float("inf"), 42.5]}, 42.5, id="skips-non-finite"),
        pytest.param({"open": [100.0], "close": [105.0]}, 100.0, id="open-preferred-over-close"),
        pytest.param({"close": [None, 99.5, 100.0]}, 99.5, id="missing-open-falls-back-to-close"),
        pytest.param(
            {"open": [None, None], "close": [None, 50.0]},
            50.0,
            id="all-null-open-falls-back-to-close",
        ),
        pytest.param(
            {"open": [0, 0], "close": [99.5]},
            99.5,
            id="zero-only-open-falls-back-to-close",
        ),
        pytest.param(
            {"open": 100.0, "close": [99.5]},
            99.5,
            id="non-list-open-falls-back-to-close",
        ),
    ],
)
def test_session_open_extraction(indicators: dict[str, Any], expected: float) -> None:
    assert _session_open(_chart_result(indicators)) == expected


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({}, id="no-indicators"),
        pytest.param({"indicators": None}, id="indicators-not-a-dict"),
        pytest.param({"indicators": {}}, id="no-quote-key"),
        pytest.param({"indicators": {"quote": []}}, id="empty-quote-list"),
        pytest.param({"indicators": {"quote": [None]}}, id="quote-entry-not-a-dict"),
        pytest.param(_chart_result({"open": [None, 0], "close": [None]}), id="all-arrays-invalid"),
    ],
)
def test_session_open_malformed_returns_none(result: dict[str, Any]) -> None:
    assert _session_open(result) is None


# --- wiring through _market_summary -----------------------------------------


def test_market_summary_includes_open_change_pct() -> None:
    asset = AssetConfig(symbol="SPY", type="equity", source="yahoo")
    bars = [_bar(_TODAY.replace(hour=0), open_=100.0)]

    summary = _market_summary(asset, _quote(103.5), bars)

    assert summary["open_change_pct"] == 3.5


def test_us_equity_open_change_survives_utc_midnight() -> None:
    # 00:30 UTC Wed 2026-11-04 is 19:30 EST Tue: the Hyperliquid-first quote
    # carries no open, so Tuesday's bar open must still anchor ΔOpen.
    quote = Quote.from_last_and_prev_close(
        symbol="AAPL",
        asset_type="equity",
        provider="hyperliquid",
        last=103.5,
        previous_close=99.0,
        timestamp=datetime(2026, 11, 4, 0, 30, tzinfo=UTC),
    )
    bars = [_bar(datetime(2026, 11, 3, 14, 30, tzinfo=UTC), open_=100.0)]

    assert _open_change_pct(quote, bars) == 3.5
