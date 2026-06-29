from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
from app.services.quotes import QuoteService


class EmptyProvider(QuoteProvider):
    name = "yahoo"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return []


class WorkingProvider(QuoteProvider):
    name = "yahoo"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return [
            Quote.from_last_and_prev_close(
                symbol=asset.symbol,
                asset_type=asset.type,
                provider="yahoo",
                last=110.0,
                previous_close=100.0,
                timestamp=datetime.now(UTC),
            )
            for asset in assets
        ]

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return []


class CountingProvider(WorkingProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        self.calls += 1
        return await super().get_quotes(assets)


class RecordingProvider(WorkingProvider):
    def __init__(self) -> None:
        self.requested_symbols: list[str] = []

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        self.requested_symbols = [asset.symbol for asset in assets]
        return await super().get_quotes(assets)


@pytest.mark.asyncio
async def test_quote_service_returns_fresh_quotes(tmp_path: Path) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="AAPL", type="equity", source="yahoo")],
        )
    ]
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": WorkingProvider()})

    grouped = await service.get_board_quotes(groups)

    assert grouped["TEST"][0].symbol == "AAPL"
    assert grouped["TEST"][0].change_pct == 10.0


@pytest.mark.asyncio
async def test_quote_service_reuses_short_lived_cache(tmp_path: Path) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="AAPL", type="equity", source="yahoo")],
        )
    ]
    provider = CountingProvider()
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": provider},
        min_refresh_seconds=60,
    )

    first = await service.get_board_quotes(groups)
    second = await service.get_board_quotes(groups)

    assert first == second
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_quote_service_requests_uncached_assets_first(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    cached = Quote.from_last_and_prev_close(
        symbol="AAPL",
        asset_type="equity",
        provider="yahoo",
        last=110.0,
        previous_close=100.0,
        timestamp=datetime.now(UTC),
    )
    db.save_quotes(database, [cached])
    groups = [
        GroupConfig(
            name="TEST",
            assets=[
                AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
                AssetConfig(symbol="XME", type="etf", source="yahoo"),
            ],
        )
    ]
    provider = RecordingProvider()
    service = QuoteService(database, {"yahoo": provider})

    await service.get_board_quotes(groups)

    assert provider.requested_symbols == ["XME", "AAPL"]


@pytest.mark.asyncio
async def test_quote_service_returns_error_quote_without_cache(tmp_path: Path) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="AAPL", type="equity", source="yahoo")],
        )
    ]
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": EmptyProvider()})

    grouped = await service.get_board_quotes(groups)

    assert grouped["TEST"][0].is_stale is True
    assert grouped["TEST"][0].error == "no_quote_available"
