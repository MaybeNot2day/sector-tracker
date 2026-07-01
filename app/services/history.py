from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from app import db
from app.models import AssetConfig, Bar, GroupConfig, ProviderName
from app.providers.base import QuoteProvider

STALE_BAR_AGE = timedelta(hours=26)
SELF_HEAL_COOLDOWN_SECONDS = 3600.0
SELF_HEAL_BATCH = 4


class HistoryService:
    def __init__(self, database_path: Path, providers: dict[ProviderName, QuoteProvider]) -> None:
        self.database_path = database_path
        self.providers = providers
        self._heal_attempts: dict[str, float] = {}

    async def get_history(
        self,
        groups: list[GroupConfig],
        symbol: str,
        *,
        interval: str,
        range_: str,
    ) -> list[Bar]:
        asset = find_asset(groups, symbol)
        if asset is None:
            return []
        provider = self.providers.get(asset.source)
        bars: list[Bar] = []
        if provider is not None:
            try:
                bars = await provider.get_history(asset, interval=interval, range_=range_)
            except Exception:
                bars = []
        if not bars and asset.type in {"equity", "etf"} and asset.source != "stooq":
            stooq = self.providers.get("stooq")
            if stooq is not None:
                try:
                    bars = await stooq.get_history(asset, interval=interval, range_=range_)
                except Exception:
                    bars = []
        if bars:
            db.save_bars(self.database_path, bars)
            return filter_bars_to_range(bars, range_)
        cached = db.load_bars(self.database_path, asset.symbol, interval, asset.source)
        if cached:
            return filter_bars_to_range(cached, range_)
        cached_any_provider = db.load_bars(self.database_path, asset.symbol, interval)
        return filter_bars_to_range(cached_any_provider, range_)

    async def refresh_stale_daily_bars(self, groups: list[GroupConfig]) -> None:
        """Opportunistically refresh the stalest daily histories.

        Serverless deployments have no background scheduler, so cached bars
        (and the daily board metrics built on them) only advance when a chart
        is opened. This picks up to SELF_HEAL_BATCH symbols whose newest 1d
        bar is older than STALE_BAR_AGE and re-fetches them; a per-symbol
        cooldown keeps weekends/holidays from re-fetching a closed market
        every poll. Called fire-and-forget from the quotes route.
        """
        newest = db.newest_bar_timestamps(self.database_path, "1d")
        now_dt = datetime.now(UTC)
        now_mono = monotonic()
        candidates: list[tuple[datetime, str]] = []
        for group in groups:
            for asset in group.assets:
                last_attempt = self._heal_attempts.get(asset.symbol, 0.0)
                if now_mono - last_attempt < SELF_HEAL_COOLDOWN_SECONDS:
                    continue
                newest_ts = newest.get(asset.symbol)
                if newest_ts is None or now_dt - newest_ts > STALE_BAR_AGE:
                    candidates.append((newest_ts or datetime.min.replace(tzinfo=UTC), asset.symbol))
        if not candidates:
            return
        candidates.sort()
        batch = [symbol for _, symbol in candidates[:SELF_HEAL_BATCH]]
        for symbol in batch:
            self._heal_attempts[symbol] = now_mono
        await asyncio.gather(
            *(
                self.get_history(groups, symbol, interval="1d", range_="1y")
                for symbol in batch
            ),
            return_exceptions=True,
        )


def find_asset(groups: list[GroupConfig], symbol: str) -> AssetConfig | None:
    wanted = symbol.upper()
    for group in groups:
        for asset in group.assets:
            if asset.symbol == wanted:
                return asset
    return None


def bars_payload(bars: list[Bar]) -> list[dict[str, object]]:
    return [
        {
            "symbol": bar.symbol,
            "provider": bar.provider,
            "interval": bar.interval,
            "timestamp": bar.timestamp.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]


def filter_bars_to_range(bars: list[Bar], range_: str) -> list[Bar]:
    if not bars:
        return bars
    end = max(_aware_timestamp(bar.timestamp) for bar in bars)
    start = _range_start(end, range_)
    if start is None:
        return bars
    return [bar for bar in bars if _aware_timestamp(bar.timestamp) >= start]


def _range_start(end: datetime, range_: str) -> datetime | None:
    if range_ == "ytd":
        return end.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    delta = {
        "10m": timedelta(minutes=10),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
        "1w": timedelta(days=7),
        "1mo": timedelta(days=31),
        "3mo": timedelta(days=93),
        "1y": timedelta(days=366),
        "5y": timedelta(days=366 * 5),
    }.get(range_)
    if delta is None:
        return None
    return end - delta


def _aware_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
