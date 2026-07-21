from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from app import db
from app.models import AssetConfig, Bar, GroupConfig, ProviderName
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider

STALE_BAR_AGE = timedelta(hours=26)
SELF_HEAL_COOLDOWN_SECONDS = 3600.0
SELF_HEAL_BATCH = 4
HISTORY_CACHE_SECONDS = 300.0
INTRADAY_CACHE_SECONDS = 15.0
HISTORY_FAILURE_CACHE_SECONDS = 15.0

# Intraday candles come from Lighter when it lists the symbol: its synthetic
# markets trade 24/7 and are not delayed. Daily/weekly history stays with the
# configured source so DMAs and 52W metrics keep official session bars.
INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h"}


class HistoryService:
    def __init__(self, database_path: Path, providers: dict[ProviderName, QuoteProvider]) -> None:
        self.database_path = database_path
        self.providers = providers
        self._heal_attempts: dict[str, float] = {}
        self._history_cache: dict[tuple[str, ProviderName, str, str], tuple[float, list[Bar]]] = {}
        self._history_locks: dict[tuple[str, ProviderName, str, str], asyncio.Lock] = {}

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
            asset = await self._tape_asset(symbol)
        if asset is None:
            return []
        cache_key = (asset.symbol, asset.source, interval, range_)
        cached = self._cached_history(cache_key, interval)
        if cached is not None:
            return cached
        lock = self._history_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cached_history(cache_key, interval)
            if cached is not None:
                return cached
            bars = await self._load_history(asset, interval=interval, range_=range_)
            self._history_cache[cache_key] = (monotonic(), bars)
            return bars

    def _cached_history(
        self,
        cache_key: tuple[str, ProviderName, str, str],
        interval: str,
    ) -> list[Bar] | None:
        cached = self._history_cache.get(cache_key)
        if cached is None:
            return None
        cached_at, bars = cached
        ttl = (
            HISTORY_FAILURE_CACHE_SECONDS
            if not bars
            else INTRADAY_CACHE_SECONDS
            if interval in INTRADAY_INTERVALS
            else HISTORY_CACHE_SECONDS
        )
        return bars if monotonic() - cached_at < ttl else None

    async def _load_history(
        self,
        asset: AssetConfig,
        *,
        interval: str,
        range_: str,
    ) -> list[Bar]:
        providers_to_try: list[QuoteProvider] = []
        lighter = self.providers.get("lighter")
        if (
            interval in INTRADAY_INTERVALS
            and asset.source != "lighter"
            and isinstance(lighter, LighterProvider)
            and await lighter.has_market(asset.symbol)
            # Ticker collisions: Lighter's ROBO is a crypto token, not the
            # robotics ETF. A Lighter market may only serve a TradFi asset's
            # candles when Lighter classifies it as a TradFi synthetic.
            and not lighter.is_crypto_market(asset.symbol)
        ):
            providers_to_try.append(lighter)
        configured = self.providers.get(asset.source)
        if configured is not None:
            providers_to_try.append(configured)
        bars: list[Bar] = []
        for provider in providers_to_try:
            try:
                bars = await provider.get_history(asset, interval=interval, range_=range_)
            except Exception:
                bars = []
            if bars:
                break
        if not bars and asset.type in {"equity", "etf"} and asset.source != "stooq":
            stooq = self.providers.get("stooq")
            if stooq is not None:
                try:
                    bars = await stooq.get_history(asset, interval=interval, range_=range_)
                except Exception:
                    bars = []
        if bars:
            await asyncio.to_thread(db.save_bars, self.database_path, bars)
            return filter_bars_to_range(bars, range_)
        cached = await asyncio.to_thread(
            db.load_bars, self.database_path, asset.symbol, interval, asset.source
        )
        if cached:
            return filter_bars_to_range(cached, range_)
        cached_any_provider = await asyncio.to_thread(
            db.load_bars, self.database_path, asset.symbol, interval
        )
        return filter_bars_to_range(cached_any_provider, range_)

    async def _tape_asset(self, symbol: str) -> AssetConfig | None:
        """Synthetic config for Lighter crypto perps outside the watchlist.

        The Markets crypto tape lists every Lighter perp, so its rows must
        chart without a YAML entry. Restricted to crypto markets: TradFi
        synthetics stay chartable only through their configured assets.
        """
        lighter = self.providers.get("lighter")
        if not isinstance(lighter, LighterProvider):
            return None
        if not await lighter.has_market(symbol) or not lighter.is_crypto_market(symbol):
            return None
        return AssetConfig(symbol=symbol.upper(), type="crypto_perp", source="lighter")

    async def refresh_stale_daily_bars(self, groups: list[GroupConfig]) -> None:
        """Opportunistically refresh the stalest daily histories.

        Serverless deployments have no background scheduler, so cached bars
        (and the daily board metrics built on them) only advance when a chart
        is opened. This picks up to SELF_HEAL_BATCH symbols whose newest 1d
        bar is older than STALE_BAR_AGE and re-fetches them; a per-symbol
        cooldown keeps weekends/holidays from re-fetching a closed market
        every poll. Awaited under a short wait_for from the quotes route.
        """
        newest = await asyncio.to_thread(db.newest_bar_timestamps, self.database_path, "1d")
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
            *(self.get_history(groups, symbol, interval="1d", range_="1y") for symbol in batch),
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
        "6mo": timedelta(days=186),
        "1y": timedelta(days=366),
        "5y": timedelta(days=366 * 5),
        "10y": timedelta(days=366 * 10),
    }.get(range_)
    if delta is None:
        return None
    return end - delta


def _aware_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
