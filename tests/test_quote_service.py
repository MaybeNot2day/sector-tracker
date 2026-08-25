import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from app import db
from app import main as main_module
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
from app.services.quotes import QuoteService, quote_payload


class EmptyProvider(QuoteProvider):
    name = "yahoo"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return []


class ExplodingProvider(EmptyProvider):
    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        raise RuntimeError("provider exploded")


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
async def test_lookup_quotes_fetch_and_short_cache(tmp_path: Path) -> None:
    provider = CountingProvider()
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": provider})
    assets = [AssetConfig(symbol="MSFT", type="equity", source="yahoo")]

    first = await service.get_lookup_quotes(assets)
    second = await service.get_lookup_quotes(assets)

    assert first["MSFT"].last == 110.0
    assert second["MSFT"] is first["MSFT"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_lookup_quotes_omit_unquotable_symbols(tmp_path: Path) -> None:
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": EmptyProvider()})

    result = await service.get_lookup_quotes(
        [AssetConfig(symbol="NOPE", type="equity", source="yahoo")]
    )

    assert result == {}


@pytest.mark.asyncio
async def test_quotes_lookup_endpoint_resolves_board_and_free_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="AAPL", type="etf", source="yahoo", name="Apple")],
        )
    ]
    monkeypatch.setattr(main_module.app.state, "groups", groups, raising=False)
    monkeypatch.setattr(main_module.app.state, "providers", {}, raising=False)
    monkeypatch.setattr(
        main_module.app.state,
        "quote_service",
        QuoteService(tmp_path / "board.sqlite3", {"yahoo": WorkingProvider()}),
        raising=False,
    )

    payload = await main_module.quotes_lookup(symbols="aapl, msft,,aapl,bad/sym")

    quotes = cast(dict[str, dict[str, object]], payload["quotes"])
    assets = cast(dict[str, dict[str, object]], payload["assets"])
    assert set(quotes) == {"AAPL", "MSFT"}
    assert quotes["AAPL"]["last"] == 110.0
    # Board config wins: the curated type and name ride along for the UI.
    assert assets["AAPL"] == {"type": "etf", "source": "yahoo", "name": "Apple"}
    assert assets["MSFT"] == {"type": "equity", "source": "yahoo", "name": None}


@pytest.mark.asyncio
async def test_quotes_lookup_endpoint_rejects_empty_symbol_set() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await main_module.quotes_lookup(symbols=",, /,")

    assert excinfo.value.status_code == 422


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

    def counted_batch_load(path: Path, symbols: Sequence[str]) -> dict[str, Quote]:
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


@pytest.mark.asyncio
async def test_quote_service_rejects_cached_symbol_with_different_asset_type(
    tmp_path: Path,
) -> None:
    database = tmp_path / "board.sqlite3"
    db.save_quotes(
        database,
        [
            Quote.from_last_and_prev_close(
                symbol="ROBO",
                asset_type="etf",
                provider="yahoo",
                last=60.0,
                previous_close=59.0,
                timestamp=datetime.now(UTC),
            )
        ],
    )
    groups = [
        GroupConfig(
            name="CRYPTO",
            assets=[AssetConfig(symbol="ROBO", type="crypto_perp", source="hyperliquid")],
        )
    ]
    service = QuoteService(database, {"hyperliquid": EmptyProvider()})

    quote = (await service.get_board_quotes(groups))["CRYPTO"][0]

    assert quote.asset_type == "crypto_perp"
    assert quote.provider == "hyperliquid"
    assert quote.last == 0.0
    assert quote.error == "no_quote_available"


@pytest.mark.asyncio
async def test_quote_service_logs_provider_exceptions(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol="AAPL", type="equity", source="yahoo")],
        )
    ]
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": ExplodingProvider()},
    )

    await service.get_board_quotes(groups)

    assert "quote fetch via yahoo failed for 1 assets" in caplog.text
    assert "provider exploded" in caplog.text


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


@pytest.mark.asyncio
async def test_stale_while_revalidate_serves_snapshot_and_refreshes_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    clock = [1_000.0]
    monkeypatch.setattr("app.services.quotes.monotonic", lambda: clock[0])

    first = await service.get_board_quotes(groups)
    assert provider.calls == 1

    # Past the refresh window: the stale snapshot answers instantly...
    clock[0] += 61.0
    second = await service.get_board_quotes(groups, allow_stale=True)
    assert second is first
    assert provider.calls == 1
    # ...and a background refresh repopulates the cache.
    for _ in range(100):
        if provider.calls == 2:
            break
        await asyncio.sleep(0.01)
    assert provider.calls == 2
    third = await service.get_board_quotes(groups)
    assert third is not first  # refreshed snapshot now serves from cache
    assert provider.calls == 2

    # Without the flag the caller pays for a synchronous refresh (serverless).
    clock[0] += 61.0
    await service.get_board_quotes(groups)
    assert provider.calls == 3

    # Beyond the stale cap even allow_stale waits for fresh data.
    clock[0] += 121.0
    await service.get_board_quotes(groups, allow_stale=True)
    assert provider.calls == 4


@pytest.mark.asyncio
async def test_quotes_route_schedules_history_heal_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class RouteQuoteService:
        async def get_board_quotes(
            self, groups: list[GroupConfig], *, allow_stale: bool = False
        ) -> dict[str, list[Quote]]:
            return {}

    class BlockingHistoryService:
        async def refresh_stale_daily_bars(self, groups: list[GroupConfig]) -> None:
            started.set()
            await release.wait()

    async def payload(
        state: object,
        groups: list[GroupConfig],
        grouped: dict[str, list[Quote]],
    ) -> dict[str, object]:
        return {"status": "ok"}

    monkeypatch.setattr(main_module.app.state, "groups", [], raising=False)
    monkeypatch.setattr(
        main_module.app.state,
        "settings",
        SimpleNamespace(enable_background_tasks=False),
        raising=False,
    )
    monkeypatch.setattr(
        main_module.app.state,
        "quote_service",
        RouteQuoteService(),
        raising=False,
    )
    monkeypatch.setattr(
        main_module.app.state,
        "history_service",
        BlockingHistoryService(),
        raising=False,
    )
    monkeypatch.setattr(main_module, "board_payload_async", payload)
    monkeypatch.setattr(main_module, "_heal_task", None)
    monkeypatch.setattr(main_module, "_heal_started", None)

    result = await asyncio.wait_for(main_module.quotes(), timeout=0.2)
    await asyncio.wait_for(started.wait(), timeout=0.2)

    assert result == {"status": "ok"}
    assert main_module._heal_task is not None
    assert not main_module._heal_task.done()

    release.set()
    await main_module._heal_task
