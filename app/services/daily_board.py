from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from time import monotonic
from typing import Any

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.services.macro import vix_read

SNAPSHOT_WRITE_INTERVAL_SECONDS = 300.0


class DailyBoardService:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._last_snapshot_write = 0.0

    def build_board(
        self,
        groups: list[GroupConfig],
        grouped_quotes: dict[str, list[Quote]],
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        """Compute overview and per-asset summaries from ONE bars load.

        Loading the bars table materializes tens of thousands of Bar objects;
        doing it once per poll instead of twice halves the hot path.
        """
        cached = db.load_bars_by_symbol(self.database_path, "1d")
        assets = _unique_assets(groups)
        quotes = _quotes_by_symbol(grouped_quotes)
        metrics = {
            symbol: _asset_metrics(
                asset,
                quotes.get(symbol),
                _preferred_bars(asset, cached),
            )
            for symbol, asset in assets.items()
        }
        summaries = {
            symbol: _market_summary(
                asset,
                quotes.get(symbol),
                _preferred_bars(asset, cached),
            )
            for symbol, asset in assets.items()
        }
        themes = _theme_metrics(groups, metrics)
        universe = _universe_metrics(metrics)
        benchmarks = _benchmark_metrics(groups, metrics)
        regime = _regime_metrics(themes, universe, benchmarks, quotes.get("^VIX"))
        rotation = _rotation_metrics(themes)
        timestamps = [quote.timestamp for quote in quotes.values()]
        overview = {
            "as_of": max(timestamps).isoformat() if timestamps else datetime.now(UTC).isoformat(),
            "regime": regime,
            "universe": universe,
            "benchmarks": benchmarks,
            "themes": themes,
            "rotation": rotation,
        }
        self._maybe_snapshot(overview)
        return overview, summaries

    def _maybe_snapshot(self, overview: dict[str, object]) -> None:
        """Persist a condensed daily snapshot, throttled per process.

        Upserts by UTC date, so intraday writes converge on the day's final
        read; history accrues one row per day for trend and delta views.
        """
        universe = overview.get("universe")
        if not isinstance(universe, dict) or not universe.get("quoted"):
            return
        now = monotonic()
        if now - self._last_snapshot_write < SNAPSHOT_WRITE_INTERVAL_SECONDS:
            return
        self._last_snapshot_write = now
        as_of = str(overview.get("as_of", ""))
        snapshot_date = as_of[:10] or datetime.now(UTC).date().isoformat()
        try:
            db.save_board_snapshot(
                self.database_path, snapshot_date, _snapshot_payload(overview)
            )
        except Exception:
            pass

    def build(
        self,
        groups: list[GroupConfig],
        grouped_quotes: dict[str, list[Quote]],
    ) -> dict[str, object]:
        overview, _ = self.build_board(groups, grouped_quotes)
        return overview

    def market_summaries(
        self,
        groups: list[GroupConfig],
        grouped_quotes: dict[str, list[Quote]],
    ) -> dict[str, dict[str, object]]:
        _, summaries = self.build_board(groups, grouped_quotes)
        return summaries


def _unique_assets(groups: list[GroupConfig]) -> dict[str, AssetConfig]:
    assets: dict[str, AssetConfig] = {}
    for group in groups:
        for asset in group.assets:
            assets.setdefault(asset.symbol, asset)
    return assets


def _quotes_by_symbol(grouped_quotes: dict[str, list[Quote]]) -> dict[str, Quote]:
    result: dict[str, Quote] = {}
    for quotes in grouped_quotes.values():
        for quote in quotes:
            result[quote.symbol] = quote
    return result


def _preferred_bars(
    asset: AssetConfig,
    cached: dict[tuple[str, str], list[Bar]],
) -> list[Bar]:
    preferred = cached.get((asset.symbol, asset.source))
    if preferred:
        return preferred
    for (symbol, _provider), bars in cached.items():
        if symbol == asset.symbol and bars:
            return bars
    return []


def _asset_metrics(
    asset: AssetConfig,
    quote: Quote | None,
    bars: list[Bar],
) -> dict[str, Any]:
    bars = _display_bars(quote, bars)
    current = _current_price(quote, bars)
    closes = [bar.close for bar in bars]
    one_day = _quote_change_pct(quote)
    five_day = _return_from_close(current, closes, 6)
    dma20 = _mean_tail(closes, 20)
    dma50 = _mean_tail(closes, 50)
    dma200 = _mean_tail(closes, 200)
    atr14 = _atr(bars, 14)

    return {
        "symbol": asset.symbol,
        "name": asset.name,
        "type": asset.type,
        "last": current,
        "change_1d": one_day,
        "change_5d": five_day,
        "above_20dma": _above(current, dma20),
        "above_50dma": _above(current, dma50),
        "above_200dma": _above(current, dma200),
        "distance_50dma": _percent_distance(current, dma50),
        "atr_extension": _ratio_distance(current, dma50, atr14),
        "high_20d": _at_high(current, bars, 20),
        "low_20d": _at_low(current, bars, 20),
        "high_52w": _at_high(current, bars, 252),
        "low_52w": _at_low(current, bars, 252),
        "has_history": bool(bars),
        "is_stale": quote.is_stale if quote else True,
    }


def _market_summary(
    asset: AssetConfig,
    quote: Quote | None,
    bars: list[Bar],
) -> dict[str, object]:
    bars = _display_bars(quote, bars)
    current = _current_price(quote, bars)
    closes = [bar.close for bar in bars]
    return {
        "sparkline": _sparkline_values(current, closes),
        "performance": {
            "1D": _quote_change_pct(quote),
            "1W": _return_from_close(current, closes, 6),
            "1M": _return_from_close(current, closes, 22),
            "3M": _return_from_close(current, closes, 64),
            "YTD": _ytd_return(current, bars),
            "1Y": _return_from_close(current, closes, 252),
        },
        "range_52w": _range_52w(current, bars),
        "rvol": _relative_volume(quote, bars),
        "has_history": bool(bars),
    }


def _relative_volume(quote: Quote | None, bars: list[Bar]) -> float | None:
    """Today's volume as a multiple of the 20-session average.

    The in-progress bar is excluded from the baseline; live quote volume is
    preferred over the cached partial bar. Partial-day readings run low by
    construction — the UI labels them accordingly.
    """
    if not bars:
        return None
    last_bar = bars[-1]
    as_of = quote.timestamp if quote is not None else datetime.now(UTC)
    last_is_today = last_bar.timestamp.astimezone(UTC).date() == as_of.astimezone(UTC).date()
    completed = bars[:-1] if last_is_today else bars
    baseline = [bar.volume for bar in completed[-20:] if bar.volume]
    if len(baseline) < 10:
        return None
    current = quote.volume if quote is not None and quote.volume else None
    if current is None and last_is_today:
        current = last_bar.volume
    if not current:
        return None
    average = fmean(baseline)
    if average <= 0:
        return None
    return round(current / average, 2)


def _quote_last(quote: Quote | None) -> float | None:
    if quote is None:
        return None
    return quote.display_last if quote.display_last is not None else quote.last


def _display_bars(quote: Quote | None, bars: list[Bar]) -> list[Bar]:
    if (
        quote is None
        or quote.display_last is None
        or quote.last <= 0
        or quote.display_last <= 0
        or not bars
    ):
        return bars
    divisor = quote.last / quote.display_last
    if divisor <= 10:
        return bars
    threshold = quote.display_last * 10
    return [
        _display_bar(bar, divisor, threshold)
        for bar in bars
    ]


def _display_bar(bar: Bar, divisor: float, threshold: float) -> Bar:
    if max(bar.open, bar.high, bar.low, bar.close) <= threshold:
        return bar
    return replace(
        bar,
        open=round(bar.open / divisor, 6),
        high=round(bar.high / divisor, 6),
        low=round(bar.low / divisor, 6),
        close=round(bar.close / divisor, 6),
    )


def _current_price(quote: Quote | None, bars: list[Bar]) -> float | None:
    quoted = _quote_last(quote)
    if quoted is not None and quoted > 0:
        return quoted
    return bars[-1].close if bars else None


def _quote_change_pct(quote: Quote | None) -> float | None:
    if quote is None:
        return None
    return quote.display_change_pct if quote.display_change_pct is not None else quote.change_pct


def _sparkline_values(current: float | None, closes: list[float], count: int = 32) -> list[float]:
    values = closes[-count:]
    if current is not None and current > 0:
        values = values[-(count - 1) :] if len(values) >= count else values
        if not values or values[-1] != current:
            values = [*values, current]
    return [round(value, 4) for value in values if value > 0]


def _range_52w(current: float | None, bars: list[Bar]) -> dict[str, float] | None:
    if current is None or current <= 0 or not bars:
        return None
    window = bars[-252:] if len(bars) >= 252 else bars
    lows = [bar.low for bar in window if bar.low > 0]
    if not lows:
        return None
    low = min(lows)
    high = max(bar.high for bar in window)
    if high <= low:
        return None
    return {
        "low": round(low, 4),
        "high": round(high, 4),
        "current": round(current, 4),
        "position_pct": round((current - low) / (high - low) * 100, 2),
        "off_low_pct": round((current - low) / low * 100, 2),
        "off_high_pct": round((high - current) / high * 100, 2),
    }


def _theme_metrics(
    groups: list[GroupConfig],
    asset_metrics: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    themes: list[dict[str, object]] = []
    for group in groups:
        members = [
            asset_metrics[asset.symbol]
            for asset in group.assets
            if asset.symbol in asset_metrics
        ]
        changes_1d = _numbers(member["change_1d"] for member in members)
        changes_5d = _numbers(member["change_5d"] for member in members)
        above_50 = _booleans(member["above_50dma"] for member in members)
        avg_1d = _average(changes_1d)
        avg_5d = _average(changes_5d)
        advance_pct = _percent(
            sum(value > 0 for value in changes_1d),
            len(changes_1d),
        )
        above_50_pct = _percent(sum(above_50), len(above_50))
        score = _theme_score(avg_1d, avg_5d, advance_pct, above_50_pct)
        themes.append(
            {
                "name": group.name,
                "count": len(group.assets),
                "score": score,
                "change_1d": avg_1d,
                "change_5d": avg_5d,
                "advance_pct": advance_pct,
                "above_50dma_pct": above_50_pct,
                "acceleration": _acceleration(avg_1d, avg_5d),
                "status": _theme_status(score),
            }
        )

    themes.sort(key=lambda item: int(item["score"]), reverse=True)
    for rank, theme in enumerate(themes, start=1):
        theme["rank"] = rank
    return themes


def _universe_metrics(asset_metrics: dict[str, dict[str, Any]]) -> dict[str, object]:
    members = list(asset_metrics.values())
    changes = _numbers(member["change_1d"] for member in members)
    above_20 = _booleans(member["above_20dma"] for member in members)
    above_50 = _booleans(member["above_50dma"] for member in members)
    above_200 = _booleans(member["above_200dma"] for member in members)

    return {
        "total": len(members),
        "quoted": len(changes),
        "history_count": sum(bool(member["has_history"]) for member in members),
        "advancers": sum(value > 0 for value in changes),
        "decliners": sum(value < 0 for value in changes),
        "unchanged": sum(value == 0 for value in changes),
        "advance_pct": _percent(sum(value > 0 for value in changes), len(changes)),
        "above_20dma_pct": _percent(sum(above_20), len(above_20)),
        "above_50dma_pct": _percent(sum(above_50), len(above_50)),
        "above_200dma_pct": _percent(sum(above_200), len(above_200)),
        "highs_20d": sum(member["high_20d"] is True for member in members),
        "lows_20d": sum(member["low_20d"] is True for member in members),
        "highs_52w": sum(member["high_52w"] is True for member in members),
        "lows_52w": sum(member["low_52w"] is True for member in members),
        "up_3pct": sum(value >= 3 for value in changes),
        "down_3pct": sum(value <= -3 for value in changes),
    }


def _benchmark_metrics(
    groups: list[GroupConfig],
    asset_metrics: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    benchmark_group = next((group for group in groups if group.name == "ETF_MACRO"), None)
    assets = benchmark_group.assets if benchmark_group else []
    return [dict(asset_metrics[asset.symbol]) for asset in assets if asset.symbol in asset_metrics]


def _regime_metrics(
    themes: list[dict[str, object]],
    universe: dict[str, object],
    benchmarks: list[dict[str, object]],
    vix_quote: Quote | None = None,
) -> dict[str, object]:
    advance_pct = _optional_number(universe.get("advance_pct"))
    above_50 = _optional_number(universe.get("above_50dma_pct"))
    benchmark_1d = _optional_number(benchmarks[0].get("change_1d")) if benchmarks else None
    positive_votes = sum(
        (
            advance_pct is not None and advance_pct >= 55,
            above_50 is not None and above_50 >= 50,
            benchmark_1d is not None and benchmark_1d > 0,
        )
    )
    negative_votes = sum(
        (
            advance_pct is not None and advance_pct <= 45,
            above_50 is not None and above_50 < 50,
            benchmark_1d is not None and benchmark_1d < 0,
        )
    )
    if positive_votes >= 2:
        direction = "RISK-ON"
    elif negative_votes >= 2:
        direction = "RISK-OFF"
    else:
        direction = "MIXED"
    breadth_is_broad = advance_pct is not None and (advance_pct >= 60 or advance_pct <= 40)
    breadth = "BROAD" if breadth_is_broad else "NARROW"
    dominant = themes[0] if themes else None
    fading = themes[-1] if themes else None
    emerging = next((theme for theme in themes if theme["status"] == "EMERGING"), None)
    if emerging is None and len(themes) > 1:
        emerging = themes[1]

    return {
        "label": f"{direction} / {breadth}",
        "tone": (
            "positive"
            if direction == "RISK-ON"
            else "negative"
            if direction == "RISK-OFF"
            else "neutral"
        ),
        "dominant": dominant,
        "emerging": emerging,
        "fading": fading,
        "vix": vix_read(vix_quote),
    }


def _snapshot_payload(overview: dict[str, object]) -> dict[str, object]:
    """Condensed overview for one snapshot row: regime, breadth, theme scores."""
    regime = overview.get("regime")
    regime = regime if isinstance(regime, dict) else {}
    universe = overview.get("universe")
    universe = universe if isinstance(universe, dict) else {}
    themes = overview.get("themes")
    themes = themes if isinstance(themes, list) else []
    return {
        "as_of": overview.get("as_of"),
        "regime": {"label": regime.get("label"), "tone": regime.get("tone")},
        "universe": {
            key: universe.get(key)
            for key in (
                "total",
                "quoted",
                "advance_pct",
                "above_20dma_pct",
                "above_50dma_pct",
                "above_200dma_pct",
                "highs_20d",
                "lows_20d",
                "up_3pct",
                "down_3pct",
            )
        },
        "themes": [
            {
                "name": theme.get("name"),
                "score": theme.get("score"),
                "change_1d": theme.get("change_1d"),
                "change_5d": theme.get("change_5d"),
                "status": theme.get("status"),
            }
            for theme in themes
            if isinstance(theme, dict)
        ],
    }


def _rotation_metrics(themes: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    ranked = sorted(
        themes,
        key=lambda item: _optional_number(item.get("acceleration")) or 0.0,
        reverse=True,
    )
    return {"climbers": ranked[:4], "fallers": list(reversed(ranked[-4:]))}


def _theme_score(
    one_day: float | None,
    five_day: float | None,
    advance_pct: float | None,
    above_50_pct: float | None,
) -> int:
    score = 50.0
    if one_day is not None:
        score += one_day * 7
    if five_day is not None:
        score += five_day * 2
    if advance_pct is not None:
        score += (advance_pct - 50) * 0.18
    if above_50_pct is not None:
        score += (above_50_pct - 50) * 0.18
    return round(max(0, min(100, score)))


def _theme_status(score: int) -> str:
    if score >= 75:
        return "DOMINANT"
    if score >= 62:
        return "STRONG"
    if score >= 52:
        return "EMERGING"
    if score >= 45:
        return "NEUTRAL"
    if score >= 30:
        return "DETERIORATING"
    return "FADING"


def _return_from_close(current: float | None, closes: list[float], offset: int) -> float | None:
    if current is None or len(closes) < offset:
        return None
    reference = closes[-offset]
    if reference == 0:
        return None
    return round((current - reference) / reference * 100, 4)


def _ytd_return(current: float | None, bars: list[Bar]) -> float | None:
    if current is None or not bars:
        return None
    end = bars[-1].timestamp
    year_bars = [bar for bar in bars if bar.timestamp.year == end.year]
    if not year_bars:
        return None
    first_year_index = bars.index(year_bars[0])
    reference = bars[first_year_index - 1].close if first_year_index > 0 else year_bars[0].close
    if reference == 0:
        return None
    return round((current - reference) / reference * 100, 4)


def _mean_tail(values: list[float], count: int) -> float | None:
    if len(values) < count:
        return None
    return fmean(values[-count:])


def _atr(bars: list[Bar], count: int) -> float | None:
    if len(bars) < count + 1:
        return None
    true_ranges: list[float] = []
    window = bars[-(count + 1) :]
    for previous, current in zip(window, window[1:], strict=False):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return fmean(true_ranges)


def _above(current: float | None, average: float | None) -> bool | None:
    if current is None or average is None:
        return None
    return current > average


def _percent_distance(current: float | None, average: float | None) -> float | None:
    if current is None or average in (None, 0):
        return None
    return round((current - average) / average * 100, 4)


def _ratio_distance(
    current: float | None,
    average: float | None,
    divisor: float | None,
) -> float | None:
    if current is None or average is None or divisor in (None, 0):
        return None
    return round((current - average) / divisor, 4)


def _at_high(current: float | None, bars: list[Bar], count: int) -> bool | None:
    if current is None or len(bars) < count:
        return None
    return current >= max(bar.high for bar in bars[-count:])


def _at_low(current: float | None, bars: list[Bar], count: int) -> bool | None:
    if current is None or len(bars) < count:
        return None
    return current <= min(bar.low for bar in bars[-count:])


def _average(values: list[float]) -> float | None:
    return round(fmean(values), 4) if values else None


def _percent(part: int, total: int) -> float | None:
    return round(part / total * 100, 1) if total else None


def _numbers(values: Iterable[object]) -> list[float]:
    return [float(value) for value in values if isinstance(value, (int, float))]


def _booleans(values: Iterable[object]) -> list[bool]:
    return [value for value in values if isinstance(value, bool)]


def _optional_number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _acceleration(one_day: float | None, five_day: float | None) -> float | None:
    if one_day is None:
        return None
    baseline = five_day / 5 if five_day is not None else 0
    return round(one_day - baseline, 4)
