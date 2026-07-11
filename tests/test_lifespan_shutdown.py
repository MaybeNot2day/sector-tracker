import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import main as main_module


class CloseTracker:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []
        self.stopped_tasks = 0
        self.all_started = asyncio.Event()


class CloseProbe:
    def __init__(self, name: str, tracker: CloseTracker, *, fail: bool = False) -> None:
        self.name = name
        self.tracker = tracker
        self.fail = fail

    async def aclose(self) -> None:
        # Network resources must not close until every background user stopped.
        assert self.tracker.stopped_tasks == 3
        self.tracker.started.append(self.name)
        if len(self.tracker.started) == 4:
            self.tracker.all_started.set()
        await self.tracker.all_started.wait()
        if self.fail:
            raise RuntimeError("close failed")
        self.tracker.completed.append(self.name)


@pytest.mark.asyncio
async def test_lifespan_stops_tasks_then_closes_every_client_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tracker = CloseTracker()
    probes = {
        "yahoo": CloseProbe("yahoo", tracker),
        "lighter": CloseProbe("lighter", tracker, fail=True),
        "stooq": CloseProbe("stooq", tracker),
        "news": CloseProbe("news", tracker),
    }
    settings = SimpleNamespace(
        watchlist_path=tmp_path / "watchlists.yaml",
        watchlist_seed_path=tmp_path / "seed.yaml",
        database_path=tmp_path / "board.db",
        database_seed_path=tmp_path / "seed.db",
        finnhub_api_key="",
        quote_poll_seconds=15,
        crypto_etf_flow_cache_seconds=300,
        news_channels=[],
        news_poll_seconds=15,
        enable_background_tasks=True,
    )

    async def idle_loop(state: Any) -> None:
        await asyncio.Event().wait()

    async def fake_stop_task(task: asyncio.Task[None]) -> None:
        tracker.stopped_tasks += 1
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_runtime_watchlist", lambda settings: None)
    monkeypatch.setattr(main_module, "ensure_runtime_database", lambda settings: None)
    monkeypatch.setattr(main_module, "load_watchlists", lambda path: [])
    monkeypatch.setattr(main_module.db, "init_db", lambda path: None)
    monkeypatch.setattr(main_module, "YahooProvider", lambda: probes["yahoo"])
    monkeypatch.setattr(main_module, "LighterProvider", lambda: probes["lighter"])
    monkeypatch.setattr(main_module, "StooqProvider", lambda: probes["stooq"])
    monkeypatch.setattr(main_module, "NewsService", lambda *args, **kwargs: probes["news"])
    monkeypatch.setattr(main_module, "QuoteService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "HistoryService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "DailyBoardService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "CryptoEtfFlowService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "AssetProfileService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "ConnectionManager", lambda: object())
    monkeypatch.setattr(main_module, "quote_poll_loop", idle_loop)
    monkeypatch.setattr(main_module, "history_refresh_loop", idle_loop)
    monkeypatch.setattr(main_module, "news_poll_loop", idle_loop)
    monkeypatch.setattr(main_module, "stop_task", fake_stop_task)
    test_app = SimpleNamespace(state=SimpleNamespace())

    async def run_lifespan() -> None:
        async with main_module.lifespan(test_app):
            assert tracker.started == []

    await asyncio.wait_for(run_lifespan(), timeout=1.0)

    assert tracker.stopped_tasks == 3
    assert set(tracker.started) == {"yahoo", "lighter", "stooq", "news"}
    # Lighter's close failure is isolated; every other resource still closes.
    assert set(tracker.completed) == {"yahoo", "stooq", "news"}
