"""Sector trend bands: normalized group performance over time.

PCPartPicker-style trend graphs for curated watchlist groups: each
constituent's daily closes are indexed to 100 at the window start, then
every session aggregates to a min/max envelope plus the equal-weight
average across members. Auto-discovered Hyperliquid listing groups stay
Markets-only. Built entirely from the daily bars the history service
already caches in SQLite — no new provider traffic.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app import db
from app.models import GroupConfig, group_category
from app.services.hyperliquid_discovery import AUTO_GROUP_NAMES

# A member must cover this share of the window's sessions to join the band;
# recently listed or sparsely-barred symbols would otherwise fake envelope
# jumps when they appear mid-window.
MIN_COVERAGE = 0.6
# Guard rail for the ?days= query param.
MIN_DAYS = 14
MAX_DAYS = 365


def group_trends_payload(path: Path, groups: list[GroupConfig], days: int) -> dict[str, object]:
    """The /api/trends payload: one min/avg/max band per curated group."""
    days = max(MIN_DAYS, min(days, MAX_DAYS))
    # Weekends and holidays thin the calendar; over-fetch so `days` sessions
    # survive, then trim to the trailing window per group.
    series_by_key = db.load_bars_by_symbol(path, "1d", limit_per_series=days + 30)

    closes: dict[str, dict[str, float]] = {}
    for (symbol, _provider), bars in series_by_key.items():
        candidate = {bar.timestamp.astimezone(UTC).date().isoformat(): bar.close for bar in bars}
        # Providers can overlap for one symbol; the longer series wins.
        if len(candidate) > len(closes.get(symbol, {})):
            closes[symbol] = candidate

    payload_groups = []
    for group in groups:
        if group.name in AUTO_GROUP_NAMES:
            continue
        trend = _group_band(group, closes, days)
        if trend is not None:
            payload_groups.append(trend)
    return {
        "as_of": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
        "groups": payload_groups,
    }


def _group_band(
    group: GroupConfig, closes: dict[str, dict[str, float]], days: int
) -> dict[str, object] | None:
    members = {
        asset.symbol: closes[asset.symbol] for asset in group.assets if closes.get(asset.symbol)
    }
    if not members:
        return None

    # Sessions most members trade: a union calendar would let 24/7 crypto
    # members alone define weekend points and whipsaw the band.
    coverage = Counter(date for series in members.values() for date in series)
    calendar = sorted(date for date, count in coverage.items() if count * 2 > len(members))[-days:]
    if len(calendar) < 2:
        return None

    indexed: dict[str, dict[str, float]] = {}
    for symbol, series in members.items():
        windowed = {date: series[date] for date in calendar if date in series}
        if len(windowed) < len(calendar) * MIN_COVERAGE:
            continue
        filled = _forward_filled(series, calendar)
        base = next(iter(filled.values()))  # first in-window close (calendar order)
        if base <= 0:
            continue
        indexed[symbol] = {date: value / base * 100 for date, value in filled.items()}
    if not indexed:
        return None

    points = []
    for date in calendar:
        values = [series[date] for series in indexed.values() if date in series]
        if not values:
            continue
        points.append(
            {
                "date": date,
                "min": round(min(values), 2),
                "max": round(max(values), 2),
                "avg": round(sum(values) / len(values), 2),
            }
        )
    if len(points) < 2:
        return None
    return {
        "name": group.name,
        "category": group_category(group),
        "members": len(indexed),
        "series": points,
    }


def _forward_filled(series: dict[str, float], calendar: list[str]) -> dict[str, float]:
    """The member's close on each calendar session, carrying the last known close."""
    history = sorted(series.items())
    filled: dict[str, float] = {}
    last: float | None = None
    index = 0
    for date in calendar:
        while index < len(history) and history[index][0] <= date:
            last = history[index][1]
            index += 1
        if last is not None:
            filled[date] = last
    return filled
