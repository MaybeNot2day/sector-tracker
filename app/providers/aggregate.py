from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from app.models import Bar

# Timeframes providers cannot serve natively get built from finer bars:
# Yahoo has no 4h resolution (aggregate 1h), some venues have no weekly/monthly
# (aggregate 1d).
_FOUR_HOURS = 4 * 3600


def aggregate_bars(bars: list[Bar], interval: str) -> list[Bar]:
    """Aggregate finer bars into 4h / 1wk / 1mo buckets.

    Bars must be in ascending timestamp order (providers return them that
    way). Each bucket keeps the first open, max high, min low, last close,
    summed volume, and the first bar's timestamp so charts anchor buckets
    at their opening time.
    """
    key = _BUCKET_KEYS.get(interval)
    if key is None or not bars:
        return bars

    aggregated: list[Bar] = []
    current_key: object = None
    for bar in bars:
        bucket = key(bar.timestamp)
        if bucket != current_key:
            aggregated.append(replace(bar, interval=interval))
            current_key = bucket
            continue
        last = aggregated[-1]
        volume = None
        if last.volume is not None or bar.volume is not None:
            volume = (last.volume or 0.0) + (bar.volume or 0.0)
        aggregated[-1] = replace(
            last,
            high=max(last.high, bar.high),
            low=min(last.low, bar.low),
            close=bar.close,
            volume=volume,
        )
    return aggregated


def _bucket_4h(timestamp: datetime) -> object:
    return int(timestamp.timestamp()) // _FOUR_HOURS


def _bucket_week(timestamp: datetime) -> object:
    calendar = timestamp.isocalendar()
    return (calendar.year, calendar.week)


def _bucket_month(timestamp: datetime) -> object:
    return (timestamp.year, timestamp.month)


_BUCKET_KEYS = {
    "4h": _bucket_4h,
    "1wk": _bucket_week,
    "1mo": _bucket_month,
}
