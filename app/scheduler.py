from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from fastapi import WebSocket

from app.providers.lighter import LighterProvider
from app.services.daily_board import crypto_breadth_metrics
from app.services.econ_calendar import any_hot_release, key_dates_payload
from app.services.macro import MACRO_TAPE_GROUP_NAME, macro_payload, with_macro_group
from app.services.quotes import grouped_quotes_payload

logger = logging.getLogger(__name__)


def _ui_version() -> str:
    """Fingerprint of the served index.html (it pins the ?v= asset busters).

    Rides every board payload so a long-lived dashboard tab can notice a
    deploy and reload itself instead of running weeks-old code against
    fresh data.
    """
    try:
        index = Path(__file__).resolve().parent / "static" / "index.html"
        return hashlib.sha256(index.read_bytes()).hexdigest()[:12]
    except OSError:  # pragma: no cover - the static bundle always ships
        return ""


UI_VERSION = _ui_version()


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()

    def register(self, websocket: WebSocket) -> None:
        # Kept separate from connect(): the WS handler sends its initial
        # snapshot first, so a concurrent broadcast can't race frame order.
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
                # Best-effort close so the dropped socket is torn down
                # instead of lingering half-open until keepalive timeout.
                with suppress(Exception):
                    await websocket.close()


async def quote_poll_loop(app_state: Any) -> None:
    await asyncio.sleep(1)
    while True:
        try:
            # One groups snapshot per cycle: a watchlist edit completing
            # mid-cycle must not zip NEW groups against OLD quotes.
            groups = app_state.groups
            grouped = await app_state.quote_service.get_board_quotes(with_macro_group(groups))
            payload = {
                "type": "quotes",
                "data": await board_payload_async(app_state, groups, grouped),
            }
            await app_state.connection_manager.broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("quote poll cycle failed")
        await asyncio.sleep(app_state.settings.quote_poll_seconds)


async def history_refresh_loop(app_state: Any) -> None:
    await asyncio.sleep(2)
    while True:
        try:
            await _refresh_daily_history(app_state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("history refresh cycle failed")
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
            logger.exception("news poll cycle failed")
        await asyncio.sleep(app_state.settings.news_poll_seconds)


# Poll cadence for the economic-calendar loop: tight around scheduled
# releases so actuals land within ~a minute, relaxed otherwise.
ECON_HOT_POLL_SECONDS = 20.0
ECON_IDLE_POLL_SECONDS = 120.0


async def econ_calendar_loop(app_state: Any) -> None:
    """Refresh key-date release enrichment and push changes over the WS.

    Broadcasting only when an actual printed or the matched set changed
    keeps the socket quiet between releases; around a scheduled print the
    loop tightens to a 20s cadence so the number lands within a minute.
    """
    await asyncio.sleep(3)
    last_state: dict[object, tuple[object, object]] | None = None
    while True:
        sleep_seconds = ECON_IDLE_POLL_SECONDS
        try:
            payload = await key_dates_payload(
                app_state.settings.database_path, app_state.econ_calendar_service
            )
            items = cast(list[dict[str, Any]], payload["key_dates"])
            state = _release_state(items)
            # The first cycle only primes the baseline: WS clients get a
            # full snapshot on connect, so there is nothing new to push.
            if last_state is not None and _release_changed(last_state, state):
                await app_state.connection_manager.broadcast(
                    {"type": "key_dates", "data": payload}
                )
            last_state = state
            if any_hot_release(items):
                sleep_seconds = ECON_HOT_POLL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("econ calendar cycle failed")
        await asyncio.sleep(sleep_seconds)


def _release_state(items: list[dict[str, Any]]) -> dict[object, tuple[object, object]]:
    """Per-item (matched_title, actual) for every enriched key-date row."""
    state: dict[object, tuple[object, object]] = {}
    for item in items:
        release = item.get("release")
        if isinstance(release, dict):
            state[item.get("id")] = (release.get("matched_title"), release.get("actual"))
    return state


def _release_changed(
    before: dict[object, tuple[object, object]],
    after: dict[object, tuple[object, object]],
) -> bool:
    """True when the matched set changed or any actual printed (null→value)."""
    if {key: value[0] for key, value in before.items()} != {
        key: value[0] for key, value in after.items()
    }:
        return True
    return any(
        before[key][1] is None and value[1] is not None
        for key, value in after.items()
        if key in before
    )


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
# Holding each dict itself (not just id()) keeps identity valid across GC.
_payload_cache: tuple[Any, dict[str, object]] | None = None
_payload_tasks: dict[int, tuple[Any, asyncio.Task[dict[str, object]]]] = {}
_payload_generation = 0


async def board_payload_async(
    app_state: Any, groups: Any, grouped: Any
) -> dict[str, object]:
    global _payload_generation
    if _payload_cache is not None and _payload_cache[0] is grouped:
        return _payload_cache[1]

    key = id(grouped)
    in_flight = _payload_tasks.get(key)
    if in_flight is not None and in_flight[0] is grouped:
        task = in_flight[1]
    else:
        _payload_generation += 1
        task = asyncio.create_task(
            _build_and_cache_payload(
                app_state, groups, grouped, _payload_generation
            )
        )
        _payload_tasks[key] = (grouped, task)
        task.add_done_callback(
            lambda finished: _discard_payload_task(key, grouped, finished)
        )

    return await asyncio.shield(task)


async def _build_and_cache_payload(
    app_state: Any, groups: Any, grouped: Any, generation: int
) -> dict[str, object]:
    global _payload_cache
    # build_board loads the full 1d bars table: keep that work off the event loop.
    payload = await asyncio.to_thread(_board_payload, app_state, groups, grouped)
    if generation == _payload_generation:
        _payload_cache = (grouped, payload)
    return payload


def _discard_payload_task(
    key: int,
    grouped: Any,
    task: asyncio.Task[dict[str, object]],
) -> None:
    in_flight = _payload_tasks.get(key)
    if in_flight is not None and in_flight[0] is grouped and in_flight[1] is task:
        del _payload_tasks[key]


def _board_payload(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
    overview, summaries = app_state.daily_board_service.build_board(groups, grouped)
    payload = grouped_quotes_payload(groups, grouped, summaries=summaries)
    lighter = app_state.providers.get("lighter")
    tape = lighter.crypto_tape_cached() if isinstance(lighter, LighterProvider) else []
    overview["crypto_breadth"] = crypto_breadth_metrics(tape)
    payload["overview"] = overview
    payload["macro"] = macro_payload(grouped.get(MACRO_TAPE_GROUP_NAME, []))
    payload["crypto_tape"] = tape
    payload["ui_version"] = UI_VERSION
    return payload


async def stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
