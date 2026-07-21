"""Timeframe routing: Yahoo period caps, Yahoo 4h aggregation path,
Lighter weekly/monthly aggregation path, and history range trimming."""

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import httpx
import pytest

from app.models import AssetConfig, Bar
from app.providers import lighter as lighter_module
from app.providers import yahoo as yahoo_module
from app.providers.lighter import LighterProvider
from app.providers.yahoo import _get_raw_history_sync, _yahoo_period
from app.services.history import filter_bars_to_range

SPY = AssetConfig(symbol="SPY", type="etf", source="yahoo")
BTC_PERP = AssetConfig(symbol="BTC", type="crypto_perp", source="lighter")


# --- _yahoo_period: board range -> chart-API range, capped per interval ---


@pytest.mark.parametrize(
    ("range_", "interval", "expected"),
    [
        # 1d over-fetches long ranges to 2y for cached DMA/52w metrics.
        ("1d", "1d", "1d"),
        ("1w", "1d", "5d"),
        ("1mo", "1d", "1mo"),
        ("3mo", "1d", "2y"),
        ("1y", "1d", "2y"),
        ("ytd", "1d", "2y"),
        # 1wk/1mo fetch deep history.
        ("5y", "1wk", "5y"),
        ("10y", "1wk", "10y"),
        ("1y", "1wk", "10y"),
        ("10y", "1mo", "10y"),
        ("1mo", "1mo", "10y"),
        # Yahoo rejects 1m beyond 7d.
        ("1d", "1m", "1d"),
        ("1w", "1m", "5d"),
        ("1mo", "1m", "5d"),
        ("1y", "1m", "5d"),
        # Yahoo rejects 5m/15m/30m beyond 60d.
        ("1d", "5m", "1d"),
        ("1w", "5m", "5d"),
        ("1mo", "5m", "1mo"),
        ("3mo", "5m", "1mo"),
        ("6mo", "15m", "1mo"),
        ("1w", "30m", "5d"),
        # 1h (also feeds the aggregated 4h path) maps honestly up to 6mo.
        ("1d", "1h", "1d"),
        ("1w", "1h", "5d"),
        ("1mo", "1h", "1mo"),
        ("3mo", "1h", "3mo"),
        ("6mo", "1h", "6mo"),
        ("1y", "1h", "6mo"),
        # Unrecognized intervals take the 1h branch.
        ("3mo", "2h", "3mo"),
        ("10y", "2h", "6mo"),
    ],
)
def test_yahoo_period_caps_range_per_interval(range_: str, interval: str, expected: str) -> None:
    assert _yahoo_period(range_, interval) == expected


# --- Yahoo 4h path: fetch native 1h bars, aggregate locally ---


def _yahoo_hourly(hour: int, o: float, h: float, low: float, c: float, v: float) -> Bar:
    return Bar(
        symbol="SPY",
        provider="yahoo",
        interval="1h",
        timestamp=datetime(2026, 6, 15, hour, tzinfo=UTC),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def test_yahoo_4h_history_fetches_1h_and_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    hourly = [
        _yahoo_hourly(12, 100.0, 101.0, 99.0, 100.5, 10.0),
        _yahoo_hourly(13, 100.5, 103.0, 100.0, 102.0, 20.0),
        _yahoo_hourly(15, 102.0, 102.5, 98.0, 99.0, 5.0),
        _yahoo_hourly(16, 99.0, 100.0, 98.5, 99.5, 8.0),
    ]

    def fake_fetch(asset: AssetConfig, interval: str, yahoo_range: str) -> list[Bar]:
        calls.append((asset.symbol, interval, yahoo_range))
        return hourly

    monkeypatch.setattr(yahoo_module, "_fetch_chart_bars", fake_fetch)

    bars = _get_raw_history_sync(SPY, "4h", "1mo")

    # Yahoo has no 4h resolution: exactly one fetch, at 1h, with the 1h
    # range cap applied.
    assert calls == [("SPY", "1h", "1mo")]
    # 12:00-15:00 UTC share the 12:00 window; 16:00 opens the next.
    assert bars == [
        Bar(
            symbol="SPY",
            provider="yahoo",
            interval="4h",
            timestamp=datetime(2026, 6, 15, 12, tzinfo=UTC),
            open=100.0,
            high=103.0,
            low=98.0,
            close=99.0,
            volume=35.0,
        ),
        Bar(
            symbol="SPY",
            provider="yahoo",
            interval="4h",
            timestamp=datetime(2026, 6, 15, 16, tzinfo=UTC),
            open=99.0,
            high=100.0,
            low=98.5,
            close=99.5,
            volume=8.0,
        ),
    ]


# --- Lighter 1wk/1mo path: fetch daily candles, aggregate locally ---


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._payload


def _install_candles(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Route /candles to a scripted payload, capturing request params."""
    requests: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
            assert url == f"{lighter_module.BASE_URL}/candles"
            requests.append(dict(params or {}))
            return _FakeResponse(payload)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return requests


def _seeded_btc_provider() -> LighterProvider:
    provider = LighterProvider()
    provider._details = {"BTC": {"symbol": "BTC", "market_id": 1, "status": "active"}}
    provider._details_time = monotonic()
    return provider


def _candle(day: datetime, o: float, h: float, low: float, c: float, v: float) -> dict[str, Any]:
    return {"t": int(day.timestamp() * 1000), "o": o, "h": h, "l": low, "c": c, "v": v}


@pytest.mark.asyncio
async def test_lighter_weekly_history_fetches_daily_and_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Thu 2025-06-05 and Fri 2025-06-06 are ISO W23; Mon 2025-06-09 is W24.
    candles = {
        "c": [
            _candle(datetime(2025, 6, 5, tzinfo=UTC), 10.0, 12.0, 9.0, 11.0, 100.0),
            _candle(datetime(2025, 6, 6, tzinfo=UTC), 11.0, 14.0, 10.0, 13.0, 50.0),
            _candle(datetime(2025, 6, 9, tzinfo=UTC), 13.0, 13.5, 12.0, 12.5, 25.0),
        ]
    }
    requests = _install_candles(monkeypatch, candles)
    provider = _seeded_btc_provider()

    bars = await provider.get_history(BTC_PERP, interval="1wk", range_="1y")

    assert len(requests) == 1
    assert requests[0]["resolution"] == "1d"
    assert requests[0]["market_id"] == 1
    assert bars == [
        Bar(
            symbol="BTC",
            provider="lighter",
            interval="1wk",
            timestamp=datetime(2025, 6, 5, tzinfo=UTC),
            open=10.0,
            high=14.0,
            low=9.0,
            close=13.0,
            volume=150.0,
        ),
        Bar(
            symbol="BTC",
            provider="lighter",
            interval="1wk",
            timestamp=datetime(2025, 6, 9, tzinfo=UTC),
            open=13.0,
            high=13.5,
            low=12.0,
            close=12.5,
            volume=25.0,
        ),
    ]


@pytest.mark.asyncio
async def test_lighter_monthly_history_fetches_daily_and_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = {
        "c": [
            _candle(datetime(2026, 1, 30, tzinfo=UTC), 5.0, 6.0, 4.0, 5.5, 10.0),
            _candle(datetime(2026, 1, 31, tzinfo=UTC), 5.5, 7.0, 5.0, 6.5, 20.0),
            _candle(datetime(2026, 2, 1, tzinfo=UTC), 6.5, 6.6, 6.0, 6.2, 30.0),
        ]
    }
    requests = _install_candles(monkeypatch, candles)
    provider = _seeded_btc_provider()

    bars = await provider.get_history(BTC_PERP, interval="1mo", range_="5y")

    assert requests[0]["resolution"] == "1d"
    assert bars == [
        Bar(
            symbol="BTC",
            provider="lighter",
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
            provider="lighter",
            interval="1mo",
            timestamp=datetime(2026, 2, 1, tzinfo=UTC),
            open=6.5,
            high=6.6,
            low=6.0,
            close=6.2,
            volume=30.0,
        ),
    ]


# --- history range trimming: new 6mo / 10y windows ---


def _daily_bar(timestamp: datetime) -> Bar:
    return Bar(
        symbol="SPY",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
    )


@pytest.mark.parametrize(
    ("range_", "window_days"),
    [("6mo", 186), ("10y", 3660)],
)
def test_filter_bars_to_range_supports_6mo_and_10y_windows(range_: str, window_days: int) -> None:
    end = datetime(2026, 7, 1, tzinfo=UTC)
    inside = end - timedelta(days=window_days)
    outside = end - timedelta(days=window_days + 1)
    bars = [_daily_bar(outside), _daily_bar(inside), _daily_bar(end)]

    filtered = filter_bars_to_range(bars, range_)

    assert [bar.timestamp for bar in filtered] == [inside, end]
