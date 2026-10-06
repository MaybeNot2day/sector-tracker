from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import db
from app.routes.status import router
from app.runtime import RuntimeMonitor


@pytest.mark.asyncio
async def test_readiness_distinguishes_liveness_from_missing_database(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(router)
    path = tmp_path / "missing.sqlite3"
    app.state.settings = SimpleNamespace(database_path=path, enable_background_tasks=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/api/health")).status_code == 200
        response = await c.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["database"]["available"] is False
        assert not path.exists()
        db.init_db(path)
        response = await c.get("/api/ready")
        assert response.status_code == 200
        assert response.json()["database"]["newest_quote_at"] is None


@pytest.mark.asyncio
async def test_readiness_tracks_cycle_failure_recovery_stall_and_task_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    app.include_router(router)
    path = tmp_path / "board.sqlite3"
    db.init_db(path)
    app.state.settings = SimpleNamespace(database_path=path, enable_background_tasks=True)
    monitor = RuntimeMonitor()
    app.state.runtime_monitor = monitor
    clock = [1000.0]
    monkeypatch.setattr("app.runtime.monotonic", lambda: clock[0])
    monitor.register("poll_task", 10)
    task = asyncio.create_task(asyncio.Event().wait())
    app.state.poll_task = task
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            with pytest.raises(RuntimeError):
                with monitor.cycle("poll_task"):
                    raise RuntimeError("upstream offline")
            response = await c.get("/api/ready")
            assert response.status_code == 200  # Cached reads remain usable during a feed outage.
            assert response.json()["loops"]["poll_task"]["last_error"] == "RuntimeError"
            with monitor.cycle("poll_task"):
                pass
            response = await c.get("/api/ready")
            assert response.json()["loops"]["poll_task"]["last_error"] is None
            assert response.json()["loops"]["poll_task"]["last_success_at"] is not None
            clock[0] += 181
            response = await c.get("/api/ready")
            assert response.status_code == 503
            assert response.json()["loops"]["poll_task"]["overdue"] is True
            with monitor.cycle("poll_task"):
                pass
            assert (await c.get("/api/ready")).status_code == 200
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            response = await c.get("/api/ready")
            assert response.status_code == 503
            assert response.json()["loops"]["poll_task"]["running"] is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
