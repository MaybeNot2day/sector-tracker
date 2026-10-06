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
    """A recent successful daily fetch can serve a chart without provider I/O."""
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(name="TEST", assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")])
    ]
    now = datetime.now(UTC)
    db.save_bars(
        database,
        [_daily_bar("SPY", now - timedelta(days=2)), _daily_bar("SPY", now)],
        fetched_at=now,
    )
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


@pytest.mark.asyncio
async def test_daily_fetch_freshness_is_not_the_session_start(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(name="TEST", assets=[AssetConfig("SPY", "etf", "yahoo")])
    ]
    now = datetime.now(UTC)
    closed_session = _daily_bar("SPY", now - timedelta(days=3))
    db.save_bars(database, [closed_session], fetched_at=now)
    provider = CountingHistoryProvider()
    service = HistoryService(database, {"yahoo": provider})

    assert await service.get_history(groups, "SPY", interval="1d", range_="1y") == [closed_session]
    assert provider.calls == 0
    # Conversely, a current-day candle fetched long ago is not fresh.
    current = _daily_bar("SPY", now.replace(hour=0, minute=0, second=0, microsecond=0))
    db.save_bars(database, [current], fetched_at=now - timedelta(hours=2))
    await service.get_history(groups, "SPY", interval="1d", range_="1y")
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_forced_refresh_bypasses_memory_and_current_day_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    now = datetime.now(UTC)
    groups = [GroupConfig("TEST", [AssetConfig("SPY", "etf", "yahoo")])]
    db.save_bars(database, [_daily_bar("SPY", now)], fetched_at=now)

    class CorrectingProvider(CountingHistoryProvider):
        async def get_history(
            self, asset: AssetConfig, *, interval: str, range_: str
        ) -> list[Bar]:
            self.calls += 1
            return [_daily_bar(asset.symbol, now, 105.0)]

    provider = CorrectingProvider()
    service = HistoryService(database, {"yahoo": provider})
    assert (await service.get_history(groups, "SPY", interval="1d", range_="1y"))[0].close == 100
    refreshed = await service.get_history(
        groups, "SPY", interval="1d", range_="1y", force_refresh=True
    )
    assert provider.calls == 1
    assert refreshed[0].close == 105.0
    assert db.load_bars(database, "SPY", "1d", "yahoo")[0].close == 105.0
    assert (await service.get_history(groups, "SPY", interval="1d", range_="1y"))[0].close == 105.0


@pytest.mark.asyncio
async def test_history_memory_cache_observes_same_timestamp_correction_and_delete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "board.sqlite3"
    now = datetime.now(UTC)
    groups = [GroupConfig("TEST", [AssetConfig("SPY", "etf", "yahoo")])]
    db.save_bars(database, [_daily_bar("SPY", now)], fetched_at=now)
    service = HistoryService(database, {"yahoo": EmptyHistoryProvider()})
    assert (await service.get_history(groups, "SPY", interval="1d", range_="1y"))[0].close == 100.0
    db.save_bars(database, [_daily_bar("SPY", now, 105.0)], fetched_at=now)
    assert (await service.get_history(groups, "SPY", interval="1d", range_="1y"))[0].close == 105.0
    with db._connect(database) as conn:
        conn.execute("DELETE FROM bars WHERE symbol = 'SPY'")
    assert await service.get_history(groups, "SPY", interval="1d", range_="1y") == []


@pytest.mark.asyncio
async def test_krx_fx_outage_keeps_one_usd_history_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.providers import yahoo

    database = tmp_path / "board.sqlite3"
    now = datetime.now(UTC)
    asset = AssetConfig("005930.KS", "equity", "yahoo")
    groups = [GroupConfig("KRX", [asset])]
    fx_available = [True]
    native = Bar(asset.symbol, "yahoo", "1d", now, 155_000, 170_500, 139_500, 155_000)
    fx = Bar("KRW=X", "yahoo", "1d", now, 1550, 1550, 1550, 1550)

    def raw_history(
        requested: AssetConfig, interval: str, range_: str
    ) -> list[Bar]:
        if requested.symbol == "KRW=X":
            return [fx] if fx_available[0] else []
        return [native]

    monkeypatch.setattr(yahoo, "_get_raw_history_sync", raw_history)
    stooq = CountingHistoryProvider()
    service = HistoryService(database, {"yahoo": yahoo.YahooProvider(), "stooq": stooq})
    first = await service.get_history(
        groups, asset.symbol, interval="1d", range_="1y", force_refresh=True
    )
    assert [bar.close for bar in first] == [100.0]
    fx_available[0] = False
    stale = await service.get_history(
        groups, asset.symbol, interval="1d", range_="1y", force_refresh=True
    )
    assert [bar.close for bar in stale] == [100.0]
    assert [bar.close for bar in db.load_bars(database, asset.symbol, "1d")] == [100.0]
    assert stooq.calls == 0


def test_daily_range_filters_use_calendar_month_and_year() -> None:
    year_end = datetime(2026, 7, 31, tzinfo=UTC)
    annual = [
        _daily_bar("BTC", datetime(2025, 7, 30, tzinfo=UTC)),
        _daily_bar("BTC", datetime(2025, 7, 31, tzinfo=UTC)),
        _daily_bar("BTC", year_end),
    ]
    assert [bar.timestamp for bar in filter_bars_to_range(annual, "1y")] == [
        datetime(2025, 7, 31, tzinfo=UTC), year_end
    ]
    monthly = [
        _daily_bar("BTC", datetime(2026, 6, 14, tzinfo=UTC)),
        _daily_bar("BTC", datetime(2026, 6, 15, tzinfo=UTC)),
        _daily_bar("BTC", datetime(2026, 7, 15, tzinfo=UTC)),
    ]
    assert [bar.timestamp for bar in filter_bars_to_range(monthly, "1mo")] == [
        datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 7, 15, tzinfo=UTC)
    ]


@pytest.mark.asyncio
async def test_krx_outage_does_not_switch_to_unlabelled_foreign_provider_cache(
    tmp_path: Path,
) -> None:
    database = tmp_path / "board.sqlite3"
    now = datetime.now(UTC)
    db.save_bars(
        database,
        [Bar("005930.KS", "stooq", "1d", now, 155_000, 170_500, 139_500, 155_000)],
    )
    asset = AssetConfig("005930.KS", "equity", "yahoo")
    service = HistoryService(database, {"yahoo": EmptyHistoryProvider()})
    assert await service.get_history(
        [GroupConfig("KRX", [asset])], asset.symbol,
        interval="1d", range_="1y", force_refresh=True,
    ) == []
