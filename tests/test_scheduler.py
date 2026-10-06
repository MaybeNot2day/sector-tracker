import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any

import pytest

from app import db, scheduler
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.services.daily_board import DailyBoardService


@pytest.fixture(autouse=True)
def reset_payload_cache() -> None:
    scheduler._payload_cache = None
    scheduler._payload_tasks.clear()
    scheduler._payload_generation = 0


@pytest.mark.asyncio
async def test_board_payload_async_shares_one_inflight_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()
    payload: dict[str, object] = {"groups": []}

    def blocked_build(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(2)
        return payload

    monkeypatch.setattr(scheduler, "_board_payload", blocked_build)
    grouped: dict[str, object] = {}
    callers = [
        asyncio.create_task(scheduler.board_payload_async(object(), [], grouped)) for _ in range(8)
    ]

    assert await asyncio.to_thread(entered.wait, 2)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*callers)

    assert calls == 1
    assert all(result is payload for result in results)
    assert await scheduler.board_payload_async(object(), [], grouped) is payload
    assert calls == 1


@pytest.mark.asyncio
async def test_board_payload_caller_cancellation_does_not_cancel_shared_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    payload: dict[str, object] = {"overview": {}}

    def blocked_build(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
        entered.set()
        assert release.wait(2)
        return payload

    monkeypatch.setattr(scheduler, "_board_payload", blocked_build)
    grouped: dict[str, object] = {}
    cancelled = asyncio.create_task(scheduler.board_payload_async(object(), [], grouped))
    survivor = asyncio.create_task(scheduler.board_payload_async(object(), [], grouped))

    assert await asyncio.to_thread(entered.wait, 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await survivor is payload
    assert await scheduler.board_payload_async(object(), [], grouped) is payload


@pytest.mark.asyncio
async def test_older_payload_build_cannot_overwrite_newer_finished_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_entered = Event()
    new_entered = Event()
    release_old = Event()
    release_new = Event()
    old_grouped: dict[str, object] = {"snapshot": "old"}
    new_grouped: dict[str, object] = {"snapshot": "new"}
    old_payload: dict[str, object] = {"snapshot": "old"}
    new_payload: dict[str, object] = {"snapshot": "new"}

    def ordered_build(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
        if grouped is old_grouped:
            old_entered.set()
            assert release_old.wait(2)
            return old_payload
        new_entered.set()
        assert release_new.wait(2)
        return new_payload

    monkeypatch.setattr(scheduler, "_board_payload", ordered_build)
    old_task = asyncio.create_task(scheduler.board_payload_async(object(), [], old_grouped))
    assert await asyncio.to_thread(old_entered.wait, 2)
    new_task = asyncio.create_task(scheduler.board_payload_async(object(), [], new_grouped))
    assert await asyncio.to_thread(new_entered.wait, 2)

    release_new.set()
    assert await new_task is new_payload
    release_old.set()
    assert await old_task is old_payload

    assert await scheduler.board_payload_async(object(), [], new_grouped) is new_payload


@pytest.mark.asyncio
async def test_board_payload_reflects_bar_corrections_with_unchanged_quotes(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    now = datetime.now(UTC)
    bar = Bar("SPY", "yahoo", "1d", now - timedelta(days=2), 100, 105, 95, 100)
    latest = replace(bar, timestamp=now - timedelta(days=1), close=102)
    db.save_bars(path, [bar, latest])
    groups = [GroupConfig("TEST", [AssetConfig("SPY", "etf", "yahoo")])]
    grouped = {
        "TEST": [
            Quote.from_last_and_prev_close(
                symbol="SPY", asset_type="etf", provider="yahoo",
                last=103, previous_close=102, timestamp=now,
            )
        ]
    }
    state = SimpleNamespace(
        settings=SimpleNamespace(database_path=path),
        providers={},
        daily_board_service=DailyBoardService(path),
    )
    before = await scheduler.board_payload_async(state, groups, grouped)
    db.save_bars(path, [replace(bar, open=90, high=95, low=85, close=90)])
    after = await scheduler.board_payload_async(state, groups, grouped)
    before_assets = before["groups"][0]["assets"]  # type: ignore[index]
    after_assets = after["groups"][0]["assets"]  # type: ignore[index]
    assert before_assets[0]["summary"]["sparkline"][0] == 100
    assert after_assets[0]["summary"]["sparkline"][0] == 90


@pytest.mark.asyncio
async def test_daily_warmer_calls_provider_with_current_day_cached_history(tmp_path: Path) -> None:
    from app.models import AssetConfig, Bar, GroupConfig, Quote
    from app.providers.base import QuoteProvider
    from app.services.history import HistoryService

    now = datetime.now(UTC)
    path = tmp_path / "board.sqlite3"
    asset = AssetConfig("SPY", "etf", "yahoo")
    groups = [GroupConfig("TEST", [asset])]
    bar = Bar("SPY", "yahoo", "1d", now, 100.0, 110.0, 90.0, 100.0)
    db.save_bars(path, [bar], fetched_at=now)

    class CorrectingProvider(QuoteProvider):
        name = "yahoo"

        def __init__(self) -> None:
            self.requested: list[str] = []

        async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
            return []

        async def get_history(
            self, asset: AssetConfig, *, interval: str, range_: str
        ) -> list[Bar]:
            self.requested.append(asset.symbol)
            return [replace(bar, close=105.0)]

    provider = CorrectingProvider()
    service = HistoryService(path, {"yahoo": provider})
    assert (await service.get_history(groups, "SPY", interval="1d", range_="1y"))[0].close == 100.0
    state = SimpleNamespace(groups=groups, history_service=service)
    await scheduler._refresh_daily_history(state)
    assert provider.requested == ["SPY"]
    assert db.load_bars(path, "SPY", "1d", "yahoo")[0].close == 105.0
