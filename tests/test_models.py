from datetime import UTC, datetime

from app.models import Quote


def test_quote_computes_change_fields_when_prev_close_exists() -> None:
    quote = Quote.from_last_and_prev_close(
        symbol="AAPL",
        asset_type="equity",
        provider="yahoo",
        last=110.0,
        previous_close=100.0,
        timestamp=datetime.now(UTC),
    )

    assert quote.change_abs == 10.0
    assert quote.change_pct == 10.0


def test_quote_handles_missing_prev_close() -> None:
    quote = Quote.from_last_and_prev_close(
        symbol="BTC",
        asset_type="crypto_perp",
        provider="hyperliquid",
        last=100_000.0,
        previous_close=None,
        timestamp=datetime.now(UTC),
    )

    assert quote.change_abs is None
    assert quote.change_pct is None

