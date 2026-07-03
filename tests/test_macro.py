from datetime import UTC, datetime

from app.models import Quote
from app.services.macro import macro_payload, vix_read

_TS = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)


def _quote(
    symbol: str,
    last: float,
    previous_close: float | None = None,
    *,
    is_stale: bool = False,
) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol=symbol,
        asset_type="index_proxy",
        provider="yahoo",
        last=last,
        previous_close=previous_close,
        timestamp=_TS,
        is_stale=is_stale,
    )


def test_tnx_cboe_x10_quote_is_rescaled() -> None:
    (item,) = macro_payload([_quote("^TNX", 44.85, 44.35)])

    assert item["unit"] == "yield"
    assert item["label"] == "US 10Y"
    assert item["last"] == 4.485
    assert item["change_abs"] == 0.05
    # Percent change is scale-invariant and must pass through untouched.
    assert item["change_pct"] == 1.127396


def test_tnx_percent_quote_passes_through() -> None:
    (item,) = macro_payload([_quote("^TNX", 4.485, 4.435)])

    assert item["last"] == 4.485
    assert item["change_abs"] == 0.05


def test_tnx_rescale_threshold_is_25() -> None:
    items = macro_payload([_quote("^TNX", 25.0), _quote("^TNX", 24.99)])

    assert [item["last"] for item in items] == [2.5, 24.99]


def test_vix_gets_inverted_tone() -> None:
    (item,) = macro_payload([_quote("^VIX", 18.4, 17.9, is_stale=True)])

    assert item["label"] == "VIX"
    assert item["unit"] == "index"
    assert item["invert_tone"] is True
    assert item["is_stale"] is True


def test_only_yield_symbols_are_rescaled() -> None:
    (item,) = macro_payload([_quote("^VIX", 44.85)])

    assert item["last"] == 44.85


def test_dxy_keeps_normal_tone_and_none_change() -> None:
    (item,) = macro_payload([_quote("DX-Y.NYB", 104.2)])

    assert item["label"] == "DXY"
    assert item["invert_tone"] is False
    assert item["change_abs"] is None


def test_non_positive_quotes_are_skipped() -> None:
    items = macro_payload(
        [_quote("^VIX", 0.0), _quote("^TNX", -1.0), _quote("DX-Y.NYB", 104.2)]
    )

    assert [item["symbol"] for item in items] == ["DX-Y.NYB"]


def test_unknown_symbol_falls_back_to_symbol_as_label() -> None:
    (item,) = macro_payload([_quote("GC=F", 2400.0)])

    assert item["label"] == "GC=F"


def test_vix_read_none_for_missing_or_non_positive_quote() -> None:
    assert vix_read(None) is None
    assert vix_read(_quote("^VIX", 0.0)) is None
    assert vix_read(_quote("^VIX", -3.0)) is None


def test_vix_read_band_boundaries() -> None:
    cases = [
        (35.5, "STRESS", "negative"),
        (28.0, "STRESS", "negative"),
        (27.99, "ELEVATED", "negative"),
        (20.0, "ELEVATED", "negative"),
        (19.99, "NORMAL", "neutral"),
        (15.0, "NORMAL", "neutral"),
        (14.99, "CALM", "positive"),
        (11.2, "CALM", "positive"),
    ]
    for level, state, tone in cases:
        read = vix_read(_quote("^VIX", level))
        assert read is not None, f"level={level}"
        assert (read["state"], read["tone"]) == (state, tone), f"level={level}"


def test_vix_read_reports_rounded_level_and_one_day_change() -> None:
    read = vix_read(_quote("^VIX", 16.456, 16.0))

    assert read == {
        "level": 16.46,
        "change_pct": 2.85,
        "state": "NORMAL",
        "tone": "neutral",
    }
