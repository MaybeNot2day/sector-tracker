"""Liveness stays cheap; readiness checks DB and background-process progress."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from app.runtime import RuntimeMonitor, database_status
from app.services.daily_board import DailyBoardService

router = APIRouter(prefix="/api")


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    payload: dict[str, object] = {"status": "ok"}
    service = getattr(request.app.state, "daily_board_service", None)
    if isinstance(service, DailyBoardService):
        payload["snapshots"] = service.snapshot_status()
    return payload


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    state = request.app.state
    settings = getattr(state, "settings", None)
    path = getattr(settings, "database_path", None)
    database = (
        await asyncio.to_thread(database_status, path)
        if path is not None
        else {"available": False, "error": "not_initialized"}
    )
    monitor = getattr(state, "runtime_monitor", None)
    loops_ready, loops = (
        monitor.payload(state) if isinstance(monitor, RuntimeMonitor) else (True, {})
    )
    ready = bool(database["available"]) and loops_ready
    return JSONResponse(
        {
            "status": "ok" if ready else "not_ready",
            "database": database,
            "background_enabled": bool(getattr(settings, "enable_background_tasks", False)),
            "loops": loops,
        },
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )
