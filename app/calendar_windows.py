from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta


def months_before(value: datetime, months: int) -> datetime:
    """Subtract calendar months, clamping month-end and leap-day anchors."""
    month_index = value.year * 12 + value.month - 1 - months
    year, month = divmod(month_index, 12)
    month += 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def range_start(end: datetime, range_: str) -> datetime | None:
    """Calendar chart windows; candle counts are not elapsed weeks or years."""
    if range_ == "ytd":
        return end.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    months = {"1mo": 1, "3mo": 3, "6mo": 6, "1y": 12, "5y": 60, "10y": 120}.get(range_)
    if months is not None:
        return months_before(end, months)
    delta = {
        "10m": timedelta(minutes=10),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
        "1w": timedelta(days=7),
    }.get(range_)
    return end - delta if delta is not None else None
