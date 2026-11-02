import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI

from app import db
from app import main as main_module


class CloseTracker:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []
        self.stopped_tasks = 0
        self.all_started = asyncio.Event()
        self.expected = 0


class CloseProbe:
    def __init__(self, name: str, tracker: CloseTracker, *, fail: bool = False) -> None:
        self.name = name
        self.tracker = tracker
        self.fail = fail

    async def aclose(self) -> None:
        # Network resources must not close until every background user stopped.
        assert self.tracker.stopped_tasks == 8
        self.tracker.started.append(self.name)
        if len(self.tracker.started) == self.tracker.expected:
            self.tracker.all_started.set()
        await self.tracker.all_started.wait()
        if self.fail:
            raise RuntimeError("close failed")
        self.tracker.completed.append(self.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_service", [None, "gpu"])
async def test_lifespan_stops_tasks_then_closes_every_client_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    broken_service: str | None,
) -> None:
    tracker = CloseTracker()
    probes: dict[str, Any] = {
        "yahoo": CloseProbe("yahoo", tracker),
        "hyperliquid": CloseProbe("hyperliquid", tracker, fail=True),
        "news": CloseProbe("news", tracker),
        "econ": CloseProbe("econ", tracker),
        "options": CloseProbe("options", tracker),
        "earnings": CloseProbe("earnings", tracker),
        "ai": CloseProbe("ai", tracker),
        "capex": CloseProbe("capex", tracker),
        "neoclouds": CloseProbe("neoclouds", tracker),
        "gpu": CloseProbe("gpu", tracker),
        "sofr": CloseProbe("sofr", tracker),
    }
    if broken_service is not None:
        # A service without aclose used to abort the eagerly-built gather
        # before any other client closed.
        probes[broken_service] = SimpleNamespace()
    tracker.expected = sum(isinstance(probe, CloseProbe) for probe in probes.values())
    settings = SimpleNamespace(
        watchlist_path=tmp_path / "watchlists.yaml",
        watchlist_seed_path=tmp_path / "seed.yaml",
        database_path=tmp_path / "board.db",
        database_seed_path=tmp_path / "seed.db",
        quote_poll_seconds=15,
        crypto_etf_flow_cache_seconds=300,
        hyperliquid_discovery_seconds=300,
        hyperliquid_discovery_group_limit=25,
        marketdata_token="token",
        marketdata_base_url="https://marketdata.test",
        options_cache_seconds=60,
        computeprices_api_key="",
        sec_user_agent="SectorTracker tests test@example.com",
        econ_calendar_cache_seconds=300,
        econ_calendar_countries="US,EU",
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
    monkeypatch.setattr(db, "init_db", lambda path: None)
    monkeypatch.setattr(main_module, "YahooProvider", lambda: probes["yahoo"])
    monkeypatch.setattr(main_module, "HyperliquidProvider", lambda: probes["hyperliquid"])
    monkeypatch.setattr(main_module, "NewsService", lambda *args, **kwargs: probes["news"])
    monkeypatch.setattr(main_module, "QuoteService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "HistoryService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "DailyBoardService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "CryptoEtfFlowService", lambda *args, **kwargs: object())
    monkeypatch.setattr(main_module, "AssetProfileService", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        main_module,
        "HyperliquidDiscoveryService",
        lambda *args, **kwargs: SimpleNamespace(merge_groups=lambda groups: groups),
    )
    monkeypatch.setattr(
        main_module, "MarketDataOptionsService", lambda *args, **kwargs: probes["options"]
    )
    monkeypatch.setattr(main_module, "ConnectionManager", lambda: object())
    monkeypatch.setattr(main_module, "quote_poll_loop", idle_loop)
    monkeypatch.setattr(main_module, "history_refresh_loop", idle_loop)
    monkeypatch.setattr(main_module, "news_poll_loop", idle_loop)
    monkeypatch.setattr(main_module, "econ_calendar_loop", idle_loop)
    monkeypatch.setattr(main_module, "earnings_warm_loop", idle_loop)
    monkeypatch.setattr(main_module, "ai_data_warm_loop", idle_loop)
    monkeypatch.setattr(main_module, "hyperliquid_discovery_loop", idle_loop)
    monkeypatch.setattr(main_module, "sofr_refresh_loop", idle_loop)
    monkeypatch.setattr(main_module, "EconCalendarService", lambda *args, **kwargs: probes["econ"])
    monkeypatch.setattr(
        main_module, "EarningsCalendarService", lambda *args, **kwargs: probes["earnings"]
    )
    monkeypatch.setattr(main_module, "AIDataService", lambda *args, **kwargs: probes["ai"])
    monkeypatch.setattr(main_module, "AICapexService", lambda *args, **kwargs: probes["capex"])
    monkeypatch.setattr(
        main_module, "NeocloudFinanceService", lambda *args, **kwargs: probes["neoclouds"]
    )
    monkeypatch.setattr(main_module, "GPUComputeService", lambda *args, **kwargs: probes["gpu"])
    monkeypatch.setattr(main_module, "SOFRService", lambda *args, **kwargs: probes["sofr"])
    monkeypatch.setattr(main_module, "stop_task", fake_stop_task)
    test_app = SimpleNamespace(state=SimpleNamespace())

    async def run_lifespan() -> None:
        async with main_module.lifespan(cast(FastAPI, test_app)):
            assert tracker.started == []

    await asyncio.wait_for(run_lifespan(), timeout=1.0)

    assert tracker.stopped_tasks == 8
    closable = {name for name, probe in probes.items() if isinstance(probe, CloseProbe)}
    assert set(tracker.started) == closable
    # Close failures (Hyperliquid raising, a service lacking aclose) are
    # isolated; every other resource still closes.
    assert set(tracker.completed) == closable - {"hyperliquid"}
    assert "yahoo" in tracker.completed and "sofr" in tracker.completed


@pytest.mark.asyncio
async def test_background_loop_crash_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail() -> None:
        raise RuntimeError("loop failed")

    task = asyncio.create_task(fail(), name="quote-poll")
    with pytest.raises(RuntimeError, match="loop failed"):
        await task

    with caplog.at_level(logging.ERROR, logger=main_module.logger.name):
        main_module._log_loop_crash(task)

    assert "background task quote-poll crashed" in caplog.text


def test_configure_logging_emits_info_with_level_and_logger_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    main_module.configure_logging()
    logging.getLogger("app.services.example").info("refreshed 3 rows")

    err = capsys.readouterr().err
    assert "INFO" in err
    assert "app.services.example" in err
    assert "refreshed 3 rows" in err
    assert len(root.handlers) == 1

    # Idempotent: a second call (reload) must not double every line.
    main_module.configure_logging()
    assert len(root.handlers) == 1


def test_configure_logging_honors_log_level_env(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setenv("LOG_LEVEL", "debug")

    main_module.configure_logging()

    assert root.level == logging.DEBUG
