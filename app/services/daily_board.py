from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from time import monotonic
from typing import Any

from app import db
from app.models import AssetConfig, Bar, GroupConfig, ProviderName, Quote
from app.services.macro import vix_read

logger = logging.getLogger(__name__)

SNAPSHOT_WRITE_INTERVAL_SECONDS = 300.0

@dataclass(frozen=True, slots=True)
class _PreparedBars:
    bars: list[Bar]
    closes: list[float]
    current: float | None


class DailyBoardService:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._last_snapshot_write = 0.0
        self._last_snapshot_success_at: datetime | None = None
        self._last_snapshot_error: str | None = None

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
        fallback_bars = _fallback_bars_by_symbol(cached)
        assets = _unique_assets(groups)
        quotes = _quotes_by_symbol(grouped_quotes)
        prepared = {
            symbol: _prepare_bars(
                quotes.get(symbol),
                cached.get((symbol, asset.source)) or fallback_bars.get(symbol, []),
            )
            for symbol, asset in assets.items()
        }
        metrics: dict[str, dict[str, Any]] = {
            symbol: _asset_metrics_prepared(asset, quotes.get(symbol), prepared[symbol])
            for symbol, asset in assets.items()
        }
        summaries: dict[str, dict[str, object]] = {
            symbol: _market_summary_prepared(asset, quotes.get(symbol), prepared[symbol])
            for symbol, asset in assets.items()
        }
        themes = _theme_metrics(groups, metrics)
        universe = _universe_metrics(metrics)
        benchmarks = _benchmark_metrics(groups, metrics)
        regime = _regime_metrics(themes, universe, benchmarks, quotes.get("^VIX"))
        rotation = _rotation_metrics(themes)
        timestamps = [quote.timestamp for quote in quotes.values()]
        overview: dict[str, object] = {
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
        # Key strictly by today's UTC date. as_of is max(quote.timestamp),
        # which during a full provider outage is the date of the last
        # successful fetch — writing under that key would silently overwrite
        # a PAST day's persisted snapshot with degraded metrics.
        today = datetime.now(UTC).date().isoformat()
        as_of_date = str(overview.get("as_of", ""))[:10]
        if as_of_date and as_of_date != today:
            return
        snapshot_date = today
        self._last_snapshot_write = now
        try:
            db.save_board_snapshot(self.database_path, snapshot_date, _snapshot_payload(overview))
        except Exception as exc:
            self._last_snapshot_error = type(exc).__name__
            logger.warning("daily board snapshot persistence failed", exc_info=True)
        else:
            self._last_snapshot_success_at = datetime.now(UTC)
            self._last_snapshot_error = None

    def snapshot_status(self) -> dict[str, object]:
        return {
            "last_success_at": (
                self._last_snapshot_success_at.isoformat()
                if self._last_snapshot_success_at is not None
                else None
            ),
            "last_error": self._last_snapshot_error,
        }

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


def _fallback_bars_by_symbol(
    cached: dict[tuple[str, ProviderName], list[Bar]],
) -> dict[str, list[Bar]]:
    """First non-empty provider series per symbol, preserving query order."""
    fallback: dict[str, list[Bar]] = {}
    for (symbol, _provider), bars in cached.items():
        if bars:
            fallback.setdefault(symbol, bars)
    return fallback


def _prepare_bars(quote: Quote | None, bars: list[Bar]) -> _PreparedBars:
    display_bars = _display_bars(quote, bars)
    return _PreparedBars(
        bars=display_bars,
        closes=[bar.close for bar in display_bars],
        current=_current_price(quote, display_bars),
    )


def _asset_metrics(
    asset: AssetConfig,
    quote: Quote | None,
    bars: list[Bar],
) -> dict[str, Any]:
    return _asset_metrics_prepared(asset, quote, _prepare_bars(quote, bars))


def _asset_metrics_prepared(
    asset: AssetConfig,
    quote: Quote | None,
    prepared: _PreparedBars,
) -> dict[str, Any]:
    bars = prepared.bars
    closes = prepared.closes
    current = prepared.current
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
    return _market_summary_prepared(asset, quote, _prepare_bars(quote, bars))


def _market_summary_prepared(
    asset: AssetConfig,
    quote: Quote | None,
    prepared: _PreparedBars,
) -> dict[str, object]:
    bars = prepared.bars
    closes = prepared.closes
    current = prepared.current
    return {
        "sparkline": _sparkline_values(current, closes),
        "performance": {
            "1D": _quote_change_pct(quote),
            "1W": _return_from_close(current, closes, 6),
            "1M": _return_from_close(current, closes, 22),
            "3M": _return_from_close(current, closes, 64),
            # No quote.timestamp here: a stale Dec-31-stamped quote in early
            # January would anchor the year backwards and report the entire
            # prior year's return as "YTD". Wall clock only.
            "YTD": _ytd_return(current, bars),
            "1Y": _return_from_close(current, closes, 252),
        },
        "range_52w": _range_52w(current, bars),
        # Yahoo's historical daily volume for futures is a different (much
        # smaller) counting regime than the live print, so the ratio is
        # meaningless there (GC=F showed 148x). No RVOL for futures.
        "rvol": None if asset.type == "future" else _relative_volume(quote, bars),
        "open_change_pct": _open_change_pct(quote, bars),
        "has_history": bool(bars),
    }


def _open_change_pct(quote: Quote | None, bars: list[Bar]) -> float | None:
    """Move since today's open (UTC day for 24/7 markets).

    Prefers the cached daily bar's open — exact and FX-normalized — and
    falls back to the quote's opening print early in the session before
    that bar exists. Non-USD quotes skip the fallback: their display
    pipeline is bar-based.
    """
    current = _current_price(quote, bars)
    if current is None or current <= 0:
        return None
    open_price = _today_bar_open(quote, bars)
    if (
        open_price is None
        and quote is not None
        and quote.currency in (None, "USD")
        and quote.open_price
        and quote.open_price > 0
    ):
        open_price = quote.open_price
    if open_price is None or open_price <= 0:
        return None
    return round((current - open_price) / open_price * 100, 4)


def _today_bar_open(quote: Quote | None, bars: list[Bar]) -> float | None:
    if not bars:
        return None
    last = bars[-1]
    as_of = quote.timestamp if quote is not None else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    bar_time = last.timestamp
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=UTC)
    if bar_time.astimezone(UTC).date() != as_of.astimezone(UTC).date():
        return None
    return last.open if last.open > 0 else None


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
    # Same naive-tzinfo hardening as _today_bar_open: cached bars can
    # come back naive, and .astimezone on a naive datetime guesses local.
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    bar_time = last_bar.timestamp
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=UTC)
    last_is_today = bar_time.astimezone(UTC).date() == as_of.astimezone(UTC).date()
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
    return [_display_bar(bar, divisor, threshold) for bar in bars]


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


# Currencies whose quote units match the cached bars without FX conversion.
# KRX-style quotes outside this set are only bar-compatible via display_last
# (bars are stored USD-converted at fetch time); if the FX leg failed and
# display fields are missing, the raw quote would divide e.g. KRW by USD.
_BAR_COMPATIBLE_CURRENCIES = frozenset({"USD", "USX"})


def _current_price(quote: Quote | None, bars: list[Bar]) -> float | None:
    quoted = _quote_last(quote)
    if quoted is not None and quoted > 0 and _bar_compatible(quote):
        return quoted
    return bars[-1].close if bars else None


def _bar_compatible(quote: Quote | None) -> bool:
    if quote is None:
        return False
    if quote.display_last is not None:
        return True
    return quote.currency is None or quote.currency in _BAR_COMPATIBLE_CURRENCIES


def _quote_change_pct(quote: Quote | None) -> float | None:
    if quote is None:
        return None
    return quote.display_change_pct if quote.display_change_pct is not None else quote.change_pct


def _sparkline_values(current: float | None, closes: list[float], count: int = 32) -> list[float]:
    values = closes[-count:]
    if current is not None and current > 0 and (not values or values[-1] != current):
        # Trim to count-1 only when the append actually happens; trimming
        # eagerly returned count-1 points when current equalled the close.
        if len(values) >= count:
            values = values[-(count - 1) :]
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
    themes: list[dict[str, Any]] = []
    for group in groups:
        members = [
            asset_metrics[asset.symbol] for asset in group.assets if asset.symbol in asset_metrics
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
        theme: dict[str, Any] = {
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
        themes.append(theme)

    themes.sort(key=lambda item: int(item["score"]), reverse=True)
    for rank, theme in enumerate(themes, start=1):
        theme["rank"] = rank
    return themes


def crypto_breadth_metrics(tape: list[dict[str, object]]) -> dict[str, object]:
    """Breadth across the full Lighter crypto tape, from quote data alone.

    Deliberately separate from the watchlist universe so 100+ alt perps
    never distort the curated regime/breadth read.
    """
    changes = _numbers(row.get("change_pct") for row in tape)
    fundings = _numbers(row.get("funding_rate") for row in tape)
    volumes = _numbers(row.get("day_volume_usd") for row in tape)
    return {
        "total": len(tape),
        "quoted": len(changes),
        "advancers": sum(value > 0 for value in changes),
        "decliners": sum(value < 0 for value in changes),
        "advance_pct": _percent(sum(value > 0 for value in changes), len(changes)),
        "up_3pct": sum(value >= 3 for value in changes),
        "down_3pct": sum(value <= -3 for value in changes),
        "up_10pct": sum(value >= 10 for value in changes),
        "down_10pct": sum(value <= -10 for value in changes),
        "median_change": round(median(changes), 4) if changes else None,
        "volume_usd": round(sum(volumes), 2) if volumes else None,
        "positive_funding_pct": _percent(sum(value > 0 for value in fundings), len(fundings)),
    }


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


def _ytd_return(
    current: float | None, bars: list[Bar], as_of: datetime | None = None
) -> float | None:
    if current is None or not bars:
        return None
    # Anchor the year to the as-of clock, not the newest cached bar: in early
    # January the cache still ends on Dec 31 and the old anchor reported the
    # ENTIRE previous year's return as "YTD".
    year = (as_of or datetime.now(UTC)).year
    year_bars = [bar for bar in bars if bar.timestamp.year == year]
    if year_bars:
        first_year_index = bars.index(year_bars[0])
        reference = bars[first_year_index - 1].close if first_year_index > 0 else year_bars[0].close
    else:
        # No bar for the current year yet: YTD is the move off the final
        # close of the prior year (the newest bar).
        reference = bars[-1].close
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
    if current is None or average is None or average == 0:
        return None
    return round((current - average) / average * 100, 4)


def _ratio_distance(
    current: float | None,
    average: float | None,
    divisor: float | None,
) -> float | None:
    if current is None or average is None or divisor is None or divisor == 0:
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
