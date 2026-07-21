"""Futures gating of relative volume in _market_summary.

Yahoo's historical daily volume for futures is a different (much smaller)
counting regime than the live print, so the ratio is meaningless there
(GC=F showed 148x). _market_summary must return rvol=None for future-typed
assets while leaving every other summary key untouched.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import AssetConfig, AssetType, Bar, Quote
from app.services.daily_board import _market_summary

_TODAY = datetime(2026, 7, 3, 15, 30, tzinfo=UTC)


def _bar(timestamp: datetime, volume: float) -> Bar:
    return Bar(
        symbol="GC=F",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=volume,
    )


def _bars() -> list[Bar]:
    """20 completed sessions (volume 100) plus today's partial bar: 21 bars.

    With the quote below, the ungated path yields rvol == 250 / 100 == 2.5,
    so a None for a future is unambiguously the gate, not missing data.
    """
    completed = [_bar(_TODAY - timedelta(days=20 - i), 100.0) for i in range(20)]
    return completed + [_bar(_TODAY.replace(hour=14), 60.0)]


def _quote(asset_type: AssetType) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol="GC=F",
        asset_type=asset_type,
        provider="yahoo",
        last=103.5,
        previous_close=99.0,
        timestamp=_TODAY,
        volume=250.0,
    )


def _summary(asset_type: AssetType) -> dict[str, object]:
    asset = AssetConfig(symbol="GC=F", type=asset_type, source="yahoo")
    return _market_summary(asset, _quote(asset_type), _bars())


def test_future_rvol_is_none_despite_computable_ratio() -> None:
    assert _summary("future")["rvol"] is None


@pytest.mark.parametrize("asset_type", ["equity", "etf"])
def test_non_future_rvol_is_computed(asset_type: AssetType) -> None:
    assert _summary(asset_type)["rvol"] == 2.5


def test_future_open_change_pct_still_computes() -> None:
    # Quote last 103.5 against today's bar open 100.0.
    assert _summary("future")["open_change_pct"] == 3.5


def test_future_gate_touches_only_rvol() -> None:
    future = _summary("future")
    equity = _summary("equity")

    assert future["rvol"] != equity["rvol"]
    assert {k: v for k, v in future.items() if k != "rvol"} == {
        k: v for k, v in equity.items() if k != "rvol"
    }
