import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
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
