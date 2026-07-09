from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastapi import WebSocket

from app.providers.lighter import LighterProvider
from app.services.daily_board import crypto_breadth_metrics
from app.services.macro import MACRO_TAPE_GROUP_NAME, macro_payload, with_macro_group
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
        """Fan out concurrently with a per-client deadline.

        Serialized sends let ONE dead client (unplugged phone, sleeping
        laptop) block every other client's updates for up to the WS
        keepalive teardown (~40s). A client that can't take the frame
        within the deadline is dropped; it reconnects on its own.
        """
        clients = list(self._clients)
        if not clients:
            return
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_json(payload), timeout=5.0) for ws in clients),
            return_exceptions=True,
        )
        for websocket, result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                self.disconnect(websocket)


async def quote_poll_loop(app_state: Any) -> None:
    await asyncio.sleep(1)
    while True:
        try:
            grouped = await app_state.quote_service.get_board_quotes(
                with_macro_group(app_state.groups)
            )
            payload = {
                "type": "quotes",
                "data": await board_payload_async(app_state, grouped),
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


async def news_poll_loop(app_state: Any) -> None:
    """Poll the Telegram previews and push new posts over the quotes WS.

    Broadcasting only when the refresh found unseen posts keeps the socket
    quiet between headlines; connected browsers see a new item roughly one
    poll interval after it lands on Telegram.
    """
    await asyncio.sleep(2)
    while True:
        try:
            new_items = await app_state.news_service.refresh()
            if new_items:
                await app_state.connection_manager.broadcast(
                    {"type": "news", "data": app_state.news_service.feed_payload()}
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(app_state.settings.news_poll_seconds)


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


# Memoized on the grouped-quotes snapshot: QuoteService returns the SAME
# dict object for the whole cache window, so identity is a correct key and
# the poll loop, HTTP route, and WS handshake share one build per window.
# Holding the dict itself (not just id()) keeps the key valid across GC.
_payload_cache: tuple[Any, dict[str, object]] | None = None


async def board_payload_async(app_state: Any, grouped: Any) -> dict[str, object]:
    global _payload_cache
    if _payload_cache is not None and _payload_cache[0] is grouped:
        return _payload_cache[1]
    # build_board loads the full 1d bars table (~75k rows, 300-550ms):
    # off the event loop, or every WS ping and HTTP response stalls.
    payload = await asyncio.to_thread(_board_payload, app_state, grouped)
    _payload_cache = (grouped, payload)
    return payload


def _board_payload(app_state: Any, grouped: Any) -> dict[str, object]:
    overview, summaries = app_state.daily_board_service.build_board(app_state.groups, grouped)
    payload = grouped_quotes_payload(app_state.groups, grouped, summaries=summaries)
    lighter = app_state.providers.get("lighter")
    tape = lighter.crypto_tape_cached() if isinstance(lighter, LighterProvider) else []
    overview["crypto_breadth"] = crypto_breadth_metrics(tape)
    payload["overview"] = overview
    payload["macro"] = macro_payload(grouped.get(MACRO_TAPE_GROUP_NAME, []))
    payload["crypto_tape"] = tape
    return payload


async def stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
