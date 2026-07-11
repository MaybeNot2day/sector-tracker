from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
from app.services.quotes import QuoteService, quote_payload


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


@pytest.mark.asyncio
async def test_quote_service_loads_cache_once_for_prioritization_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            name="ONE",
            assets=[
                AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
                AssetConfig(symbol="XME", type="etf", source="yahoo"),
            ],
        ),
        GroupConfig(
            name="TWO",
            assets=[AssetConfig(symbol="AAPL", type="equity", source="yahoo")],
        ),
    ]
    original_batch_load = db.load_latest_quotes
    batch_calls: list[set[str]] = []

    def counted_batch_load(path: Path, symbols: set[str]) -> dict[str, Quote]:
        batch_calls.append(set(symbols))
        return original_batch_load(path, symbols)

    def forbidden_single_load(path: Path, symbol: str) -> Quote | None:
        raise AssertionError(f"unexpected per-symbol cache query for {symbol} at {path}")

    monkeypatch.setattr(db, "load_latest_quotes", counted_batch_load)
    monkeypatch.setattr(db, "load_latest_quote", forbidden_single_load)
    service = QuoteService(database, {"yahoo": EmptyProvider()})

    grouped = await service.get_board_quotes(groups)

    assert batch_calls == [{"AAPL", "XME"}]
    assert grouped["ONE"][0] == db.mark_stale(cached)
    assert grouped["ONE"][1].error == "no_quote_available"
    assert grouped["TWO"][0] == db.mark_stale(cached)


def test_quote_payload_exposes_display_fields() -> None:
    quote = Quote.from_last_and_prev_close(
        symbol="005930.KS",
        asset_type="equity",
        provider="yahoo",
        last=314_500,
        previous_close=334_000,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        currency="KRW",
        display_last=202.9,
        display_previous_close=216.9,
        display_change_abs=-14.0,
        display_change_pct=-6.45,
        display_currency="USD",
    )

    payload = quote_payload(quote)

    assert payload["last"] == 314_500
    assert payload["currency"] == "KRW"
    assert payload["display_last"] == 202.9
    assert payload["display_currency"] == "USD"
