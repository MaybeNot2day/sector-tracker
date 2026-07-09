from datetime import UTC, datetime, timedelta

import pytest

from app.models import AssetConfig, Bar, Quote
from app.services.daily_board import _asset_metrics, _bar_compatible, _current_price

_NOW = datetime(2026, 7, 9, 15, 30, tzinfo=UTC)


def _bar(timestamp: datetime, close: float) -> Bar:
    return Bar(
        symbol="373220.KS",
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
    )


def _usd_bars(count: int = 60) -> list[Bar]:
    """Ascending USD-converted daily bars closing at exactly 140.0."""
    return [_bar(_NOW - timedelta(days=count - i), 125.25 + 0.25 * i) for i in range(count)]


def _quote(last: float, currency: str | None, display_last: float | None = None) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol="373220.KS",
        asset_type="equity",
        provider="yahoo",
        last=last,
        previous_close=last,
        timestamp=_NOW,
        currency=currency,
        display_last=display_last,
    )


@pytest.mark.parametrize(
    ("currency", "display_last", "expected"),
    [
        # KRW quote whose FX leg failed: raw won is NOT on the USD bar scale.
        ("KRW", None, False),
        # The USD display price restores compatibility for the same quote.
        ("KRW", 140.5, True),
        ("USD", None, True),
        ("USX", None, True),
        (None, None, True),
    ],
)
def test_bar_compatibility_by_currency_and_display(
    currency: str | None, display_last: float | None, expected: bool
) -> None:
    assert _bar_compatible(_quote(192_500.0, currency, display_last)) is expected


def test_krw_quote_without_display_falls_back_to_bar_close() -> None:
    # 192,500 KRW must never be compared against ~140 USD bars.
    assert _current_price(_quote(192_500.0, "KRW"), _usd_bars()) == 140.0


def test_krw_quote_with_display_uses_display_price() -> None:
    assert _current_price(_quote(192_500.0, "KRW", display_last=141.25), _usd_bars()) == 141.25


@pytest.mark.parametrize("currency", ["USD", "USX", None])
def test_compatible_currency_quote_wins_over_bars(currency: str | None) -> None:
    assert _current_price(_quote(139.0, currency), _usd_bars()) == 139.0


def test_krw_quote_without_display_and_without_bars_returns_none() -> None:
    assert _current_price(_quote(192_500.0, "KRW"), []) is None


def test_missing_quote_falls_back_to_bar_close() -> None:
    assert _current_price(None, _usd_bars()) == 140.0


def test_asset_metrics_ignore_raw_krw_last_and_stay_on_bar_scale() -> None:
    # The regression: a KRW quote with no display fields fed 192,500 into
    # every derived metric, reporting ~+137,000% moves against USD bars.
    asset = AssetConfig(symbol="373220.KS", type="equity", source="yahoo")

    metrics = _asset_metrics(asset, _quote(192_500.0, "KRW"), _usd_bars())

    assert metrics["last"] == 140.0
    assert metrics["change_5d"] == 0.9009  # vs closes[-6] == 138.75
    assert metrics["distance_50dma"] == 4.5752  # vs 50dma == 133.875
    assert metrics["above_50dma"] is True


def test_asset_metrics_krw_quote_with_display_uses_display_price() -> None:
    asset = AssetConfig(symbol="373220.KS", type="equity", source="yahoo")

    metrics = _asset_metrics(asset, _quote(192_500.0, "KRW", display_last=141.25), _usd_bars())

    assert metrics["last"] == 141.25


def test_asset_metrics_usd_quote_keeps_quote_last() -> None:
    asset = AssetConfig(symbol="SPY", type="etf", source="yahoo")

    metrics = _asset_metrics(asset, _quote(139.0, "USD"), _usd_bars())

    assert metrics["last"] == 139.0
