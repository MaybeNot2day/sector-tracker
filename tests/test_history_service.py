import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app import db
from app.models import AssetConfig, Bar, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.services import history as history_module
from app.services.history import HistoryService, filter_bars_to_range


class HistoryProvider(QuoteProvider):
    name = "yahoo"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return [
            Bar(
                symbol=asset.symbol,
                provider="yahoo",
                interval=interval,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                open=100.0,
                high=105.0,
                low=95.0,
                close=102.0,
            )
        ]


class CountingHistoryProvider(HistoryProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def get_history(
        self,
        asset: AssetConfig,
        *,
        interval: str,
        range_: str,
    ) -> list[Bar]:
        self.calls += 1
        await asyncio.sleep(0)
        return await super().get_history(asset, interval=interval, range_=range_)


class EmptyHistoryProvider(HistoryProvider):
    async def get_history(
        self,
        asset: AssetConfig,
        *,
        interval: str,
        range_: str,
    ) -> list[Bar]:
        return []


@pytest.mark.asyncio
async def test_history_service_fetches_and_caches_bars(tmp_path: Path) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")],
        )
    ]
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": HistoryProvider()})

    bars = await service.get_history(groups, "SPY", interval="1d", range_="1y")

    assert len(bars) == 1
    assert bars[0].close == 102.0


def _daily_bar(symbol: str, timestamp: datetime, close: float = 100.0) -> Bar:
    return Bar(
        symbol=symbol,
        provider="yahoo",
        interval="1d",
        timestamp=timestamp,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
    )


@pytest.mark.asyncio
async def test_fresh_cached_daily_bars_skip_live_providers(tmp_path: Path) -> None:
    """A chart open must not wait on a provider when SQLite already holds a
    fresh (<26h) daily series — the hourly warm loop keeps it current."""
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(name="TEST", assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")])
    ]
    now = datetime.now(UTC)
    db.save_bars(database, [_daily_bar("SPY", now - timedelta(days=2)), _daily_bar("SPY", now)])
    provider = CountingHistoryProvider()
    service = HistoryService(database, {"yahoo": provider})

    bars = await service.get_history(groups, "SPY", interval="1d", range_="1y")

    assert len(bars) == 2
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_stale_cached_daily_bars_still_fetch_live(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(name="TEST", assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")])
    ]
    db.save_bars(database, [_daily_bar("SPY", datetime.now(UTC) - timedelta(days=4))])
    provider = CountingHistoryProvider()
    service = HistoryService(database, {"yahoo": provider})

    await service.get_history(groups, "SPY", interval="1d", range_="1y")

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_history_service_fetches_unconfigured_explicit_fallback(
    tmp_path: Path,
) -> None:
    provider = CountingHistoryProvider()
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": provider})
    fallback = AssetConfig(symbol="CIFR", type="equity", source="yahoo")

    bars = await service.get_history(
        [],
        "CIFR",
        interval="1d",
        range_="1y",
        fallback_asset=fallback,
    )

    assert provider.calls == 1
    assert [bar.symbol for bar in bars] == ["CIFR"]


@pytest.mark.asyncio
async def test_history_service_collapses_concurrent_identical_fetches(tmp_path: Path) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")],
        )
    ]
    provider = CountingHistoryProvider()
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": provider})

    results = await asyncio.gather(
        *(service.get_history(groups, "SPY", interval="1d", range_="1y") for _ in range(3))
    )

    assert provider.calls == 1
    assert [len(bars) for bars in results] == [1, 1, 1]


def test_filter_bars_to_intraday_range() -> None:
    bars = [
        Bar(
            symbol="SPY",
            provider="yahoo",
            interval="1m",
            timestamp=datetime(2026, 1, 1, 10, minute, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
        )
        for minute in range(20)
    ]

    filtered = filter_bars_to_range(bars, "10m")

    assert filtered[0].timestamp == datetime(2026, 1, 1, 10, 9, tzinfo=UTC)
    assert filtered[-1].timestamp == datetime(2026, 1, 1, 10, 19, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sqlite_fallback_chooses_one_provider_series(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")],
        )
    ]
    base = datetime(2026, 1, 1, tzinfo=UTC)
    cached = [
        Bar(
            symbol="SPY",
            provider=cast(ProviderName, provider),
            interval="1d",
            timestamp=base + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
        )
        for index, (provider, close) in enumerate(
            [("yahoo", 100.0), ("stooq", 90.0), ("yahoo", 101.0)]
        )
    ]

    def load_bars(path: Path, symbol: str, interval: str, provider: str | None = None) -> list[Bar]:
        return [] if provider is not None else cached

    monkeypatch.setattr(db, "load_bars", load_bars)
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": EmptyHistoryProvider()})

    bars = await service.get_history(groups, "SPY", interval="1d", range_="1y")

    assert [bar.provider for bar in bars] == ["yahoo", "yahoo"]
    assert [bar.close for bar in bars] == [100.0, 101.0]


def test_history_cache_evicts_completed_old_keys(tmp_path: Path) -> None:
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": EmptyHistoryProvider()})
    for index in range(history_module.HISTORY_CACHE_MAX + 1):
        key: tuple[str, ProviderName, str, str] = (f"SYM{index}", "yahoo", "1d", "1y")
        service._history_cache[key] = (float(index), [])
        service._history_locks[key] = asyncio.Lock()

    service._evict_history_cache()

    assert len(service._history_cache) == history_module.HISTORY_CACHE_MAX
    assert set(service._history_locks) == set(service._history_cache)


@pytest.mark.asyncio
async def test_self_heal_extends_backoff_when_newest_bar_does_not_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10_000.0]
    stale = datetime.now(UTC) - timedelta(days=3)
    monkeypatch.setattr(history_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        db,
        "newest_bar_timestamps",
        lambda path, interval: {"SPY": stale},
    )
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")],
        )
    ]
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": EmptyHistoryProvider()})
    calls = 0

    async def record_history(*args: object, **kwargs: object) -> list[Bar]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(service, "get_history", record_history)

    await service.refresh_stale_daily_bars(groups)
    clock[0] += history_module.SELF_HEAL_COOLDOWN_SECONDS + 1
    await service.refresh_stale_daily_bars(groups)

    assert calls == 1
