from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastapi import WebSocket

from app.services.quotes import grouped_quotes_payload


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale_clients: list[WebSocket] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(payload)
            except Exception:
                stale_clients.append(websocket)
        for websocket in stale_clients:
            self.disconnect(websocket)


async def quote_poll_loop(app_state: Any) -> None:
    await asyncio.sleep(1)
    while True:
        try:
            grouped = await app_state.quote_service.get_board_quotes(app_state.groups)
            payload = {
                "type": "quotes",
                "data": _board_payload(app_state, grouped),
            }
            await app_state.connection_manager.broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(app_state.settings.quote_poll_seconds)


async def history_refresh_loop(app_state: Any) -> None:
    await asyncio.sleep(2)
    while True:
        try:
            await _refresh_daily_history(app_state)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(app_state.settings.history_refresh_seconds)


async def _refresh_daily_history(app_state: Any) -> None:
    semaphore = asyncio.Semaphore(6)
    symbols = list(
        dict.fromkeys(asset.symbol for group in app_state.groups for asset in group.assets)
    )

    async def refresh(symbol: str) -> None:
        async with semaphore:
            await app_state.history_service.get_history(
                app_state.groups,
                symbol,
                interval="1d",
                range_="1y",
            )

    await asyncio.gather(*(refresh(symbol) for symbol in symbols))


def _board_payload(app_state: Any, grouped: Any) -> dict[str, object]:
    summaries = app_state.daily_board_service.market_summaries(app_state.groups, grouped)
    payload = grouped_quotes_payload(app_state.groups, grouped, summaries=summaries)
    payload["overview"] = app_state.daily_board_service.build(app_state.groups, grouped)
    return payload


async def stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
