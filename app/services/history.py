from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from app import db
from app.models import AssetConfig, Bar, GroupConfig, ProviderName
from app.providers.base import QuoteProvider
from app.providers.hyperliquid import HyperliquidProvider

STALE_BAR_AGE = timedelta(hours=26)
SELF_HEAL_COOLDOWN_SECONDS = 3600.0
SELF_HEAL_NO_PROGRESS_COOLDOWN_SECONDS = 6 * 3600.0
SELF_HEAL_BATCH = 4
HISTORY_CACHE_SECONDS = 300.0
INTRADAY_CACHE_SECONDS = 15.0
HISTORY_FAILURE_CACHE_SECONDS = 15.0
HISTORY_CACHE_MAX = 256

# Intraday candles come from Hyperliquid when it lists the symbol: its synthetic
# markets trade 24/7 and are not delayed. Daily/weekly history stays with the
# configured source so DMAs and 52W metrics keep official session bars.
INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h"}


class HistoryService:
    def __init__(self, database_path: Path, providers: dict[ProviderName, QuoteProvider]) -> None:
        self.database_path = database_path
        self.providers = providers
        # symbol -> (attempt monotonic time, newest bar observed before it)
        self._heal_attempts: dict[str, tuple[float, datetime | None]] = {}
        self._history_cache: dict[tuple[str, ProviderName, str, str], tuple[float, list[Bar]]] = {}
        self._history_locks: dict[tuple[str, ProviderName, str, str], asyncio.Lock] = {}

    async def get_history(
        self,
        groups: list[GroupConfig],
        symbol: str,
        *,
        interval: str,
        range_: str,
        fallback_asset: AssetConfig | None = None,
    ) -> list[Bar]:
        asset = find_asset(groups, symbol)
        if asset is None:
            asset = await self._tape_asset(symbol)
        if asset is None:
            asset = fallback_asset
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
            self._evict_history_cache()
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
        ttl = _history_ttl(interval, bars)
        return bars if monotonic() - cached_at < ttl else None

    def _evict_history_cache(self) -> None:
        """Bound completed request keys without splitting concurrent fetches."""
        if len(self._history_cache) <= HISTORY_CACHE_MAX:
            return
        now = monotonic()
        expired = [
            key
            for key, (cached_at, bars) in self._history_cache.items()
            if now - cached_at >= _history_ttl(key[2], bars)
        ]
        ordered = expired + [
            key
            for key, _ in sorted(
                self._history_cache.items(),
                key=lambda item: item[1][0],
            )
            if key not in expired
        ]
        for key in ordered:
            if len(self._history_cache) <= HISTORY_CACHE_MAX:
                break
            lock = self._history_locks.get(key)
            if lock is not None and lock.locked():
                continue
            self._history_cache.pop(key, None)
            self._history_locks.pop(key, None)

    async def _load_history(
        self,
        asset: AssetConfig,
        *,
        interval: str,
        range_: str,
    ) -> list[Bar]:
        providers_to_try: list[QuoteProvider] = []
        hyperliquid = self.providers.get("hyperliquid")
        if (
            interval in INTRADAY_INTERVALS
            and asset.source != "hyperliquid"
            and isinstance(hyperliquid, HyperliquidProvider)
            and await hyperliquid.has_market(asset.symbol)
            # Ticker collisions: Hyperliquid's ROBO is a crypto token, not the
            # robotics ETF. A Hyperliquid market may only serve a TradFi asset's
            # candles when Hyperliquid classifies it as a TradFi synthetic.
            and not hyperliquid.is_crypto_market(asset.symbol)
        ):
            providers_to_try.append(hyperliquid)
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
        return filter_bars_to_range(_largest_provider_series(cached_any_provider), range_)

    async def _tape_asset(self, symbol: str) -> AssetConfig | None:
        """Synthetic config for Hyperliquid markets outside the watchlist.

        The Markets crypto tape lists every Hyperliquid perp, and the Watch
        grid accepts free-typed symbols — both must chart without a YAML
        entry. Crypto markets chart as perps; xyz TradFi synthetics chart as
        equities so the whole Hyperliquid universe stays reachable.
        """
        hyperliquid = self.providers.get("hyperliquid")
        if not isinstance(hyperliquid, HyperliquidProvider):
            return None
        if not await hyperliquid.has_market(symbol):
            return None
        if hyperliquid.is_crypto_market(symbol):
            return AssetConfig(symbol=symbol.upper(), type="crypto_perp", source="hyperliquid")
        if hyperliquid.is_tradfi_market(symbol):
            return AssetConfig(symbol=symbol.upper(), type="equity", source="hyperliquid")
        return None

    async def refresh_stale_daily_bars(self, groups: list[GroupConfig]) -> None:
        """Opportunistically refresh the stalest daily histories.

        Serverless deployments have no background scheduler, so cached bars
        (and the daily board metrics built on them) only advance when a chart
        is opened. This picks up to SELF_HEAL_BATCH symbols whose newest 1d
        bar is older than STALE_BAR_AGE and re-fetches them; a per-symbol
        backoff expands when a closed market produces no new bar. The quotes
        route starts this work in the background and never waits for it.
        """
        newest = await asyncio.to_thread(db.newest_bar_timestamps, self.database_path, "1d")
        now_dt = datetime.now(UTC)
        now_mono = monotonic()
        candidates: list[tuple[datetime, str]] = []
        for group in groups:
            for asset in group.assets:
                newest_ts = newest.get(asset.symbol)
                previous_attempt = self._heal_attempts.get(asset.symbol)
                if previous_attempt is not None:
                    attempted_at, observed_ts = previous_attempt
                    cooldown = (
                        SELF_HEAL_NO_PROGRESS_COOLDOWN_SECONDS
                        if newest_ts == observed_ts
                        else SELF_HEAL_COOLDOWN_SECONDS
                    )
                    if now_mono - attempted_at < cooldown:
                        continue
                if newest_ts is None or now_dt - newest_ts > STALE_BAR_AGE:
                    candidates.append((newest_ts or datetime.min.replace(tzinfo=UTC), asset.symbol))
        if not candidates:
            return
        candidates.sort()
        batch = [symbol for _, symbol in candidates[:SELF_HEAL_BATCH]]
        for symbol in batch:
            self._heal_attempts[symbol] = (now_mono, newest.get(symbol))
        await asyncio.gather(
            *(self.get_history(groups, symbol, interval="1d", range_="1y") for symbol in batch),
            return_exceptions=True,
        )


def _history_ttl(interval: str, bars: list[Bar]) -> float:
    if not bars:
        return HISTORY_FAILURE_CACHE_SECONDS
    if interval in INTRADAY_INTERVALS:
        return INTRADAY_CACHE_SECONDS
    return HISTORY_CACHE_SECONDS


def _largest_provider_series(bars: list[Bar]) -> list[Bar]:
    """Choose one coherent provider series instead of interleaving candles."""
    if not bars:
        return []
    by_provider: dict[ProviderName, list[Bar]] = {}
    for bar in bars:
        by_provider.setdefault(bar.provider, []).append(bar)
    return max(
        by_provider.values(),
        key=lambda series: (
            len(series),
            max(_aware_timestamp(bar.timestamp) for bar in series),
        ),
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
