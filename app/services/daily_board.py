from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote


class DailyBoardService:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def build(
        self,
        groups: list[GroupConfig],
        grouped_quotes: dict[str, list[Quote]],
    ) -> dict[str, object]:
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
        themes = _theme_metrics(groups, metrics)
        universe = _universe_metrics(metrics)
        benchmarks = _benchmark_metrics(groups, metrics)
        regime = _regime_metrics(themes, universe, benchmarks)
        rotation = _rotation_metrics(themes)
        timestamps = [quote.timestamp for quote in quotes.values()]

        return {
            "as_of": max(timestamps).isoformat() if timestamps else datetime.now(UTC).isoformat(),
            "regime": regime,
            "universe": universe,
            "benchmarks": benchmarks,
            "themes": themes,
            "rotation": rotation,
        }

    def market_summaries(
        self,
        groups: list[GroupConfig],
        grouped_quotes: dict[str, list[Quote]],
    ) -> dict[str, dict[str, object]]:
        cached = db.load_bars_by_symbol(self.database_path, "1d")
        assets = _unique_assets(groups)
        quotes = _quotes_by_symbol(grouped_quotes)
        return {
            symbol: _market_summary(
                asset,
                quotes.get(symbol),
                _preferred_bars(asset, cached),
            )
            for symbol, asset in assets.items()
        }


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
    current = quote.last if quote and quote.last > 0 else (bars[-1].close if bars else None)
    closes = [bar.close for bar in bars]
    one_day = quote.change_pct if quote else None
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
    current = quote.last if quote and quote.last > 0 else (bars[-1].close if bars else None)
    closes = [bar.close for bar in bars]
    return {
        "sparkline": _sparkline_values(current, closes),
        "performance": {
            "1D": quote.change_pct if quote else None,
            "1W": _return_from_close(current, closes, 6),
            "1M": _return_from_close(current, closes, 22),
            "3M": _return_from_close(current, closes, 64),
            "YTD": _ytd_return(current, bars),
            "1Y": _return_from_close(current, closes, 252),
        },
        "range_52w": _range_52w(current, bars),
        "has_history": bool(bars),
    }


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
    low = min(bar.low for bar in window)
    high = max(bar.high for bar in window)
    if low <= 0 or high <= low:
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
