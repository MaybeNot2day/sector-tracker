from __future__ import annotations

from app.models import AssetConfig, GroupConfig, Quote

MACRO_TAPE_GROUP_NAME = "MACRO_TAPE"

# Context symbols polled alongside the watchlists but excluded from the
# universe, breadth, and Markets grid. ^TNX quotes the 10Y yield x10.
MACRO_TAPE_GROUP = GroupConfig(
    name=MACRO_TAPE_GROUP_NAME,
    assets=[
        AssetConfig(symbol="^VIX", type="index_proxy", source="yahoo", name="VIX"),
        AssetConfig(symbol="DX-Y.NYB", type="index_proxy", source="yahoo", name="DXY"),
        AssetConfig(symbol="^TNX", type="index_proxy", source="yahoo", name="US 10Y"),
    ],
)

_YIELD_SYMBOLS = {"^TNX"}
# VIX up is risk-off; invert the green/red tone on the strip.
_INVERTED_TONE_SYMBOLS = {"^VIX"}


def with_macro_group(groups: list[GroupConfig]) -> list[GroupConfig]:
    return [*groups, MACRO_TAPE_GROUP]


def macro_payload(quotes: list[Quote]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    label_by_symbol = {asset.symbol: asset.name for asset in MACRO_TAPE_GROUP.assets}
    for quote in quotes:
        if quote.last <= 0:
            continue
        is_yield = quote.symbol in _YIELD_SYMBOLS
        # Yahoo quotes ^TNX as the percent yield (4.49); the CBOE x10
        # convention (44.9) shows up in some feeds, so rescale defensively.
        divisor = 10.0 if is_yield and quote.last >= 25 else 1.0
        items.append(
            {
                "symbol": quote.symbol,
                "label": label_by_symbol.get(quote.symbol) or quote.symbol,
                "unit": "yield" if is_yield else "index",
                "last": round(quote.last / divisor, 4),
                "change_abs": (
                    round(quote.change_abs / divisor, 4)
                    if quote.change_abs is not None
                    else None
                ),
                "change_pct": quote.change_pct,
                "invert_tone": quote.symbol in _INVERTED_TONE_SYMBOLS,
                "is_stale": quote.is_stale,
            }
        )
    return items


def vix_read(quote: Quote | None) -> dict[str, object] | None:
    """Volatility state for the regime panel: level bands plus 1D direction."""
    if quote is None or quote.last <= 0:
        return None
    level = quote.last
    if level >= 28:
        state, tone = "STRESS", "negative"
    elif level >= 20:
        state, tone = "ELEVATED", "negative"
    elif level >= 15:
        state, tone = "NORMAL", "neutral"
    else:
        state, tone = "CALM", "positive"
    return {
        "level": round(level, 2),
        "change_pct": quote.change_pct,
        "state": state,
        "tone": tone,
    }
