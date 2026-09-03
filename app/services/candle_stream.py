"""Live candle streaming from the Hyperliquid websocket.

One upstream WS connection multiplexes every (symbol, interval) the open
chart modals subscribe to; frames route to per-client queues that the
/ws/candles endpoint drains. The upstream connection exists only while at
least one subscription is active, and resubscribes everything after a
reconnect. Only Hyperliquid markets can stream — the endpoint refuses
symbols without a market so the frontend keeps its REST chart.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from app.providers.hyperliquid import HyperliquidProvider

logger = logging.getLogger(__name__)

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
# Intervals the chart modal offers; the upstream supports more, but the
# endpoint surface stays this small on purpose.
STREAM_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})
RECONNECT_BASE_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
# A stalled client must not grow the queue without bound; candle frames are
# near-continuous during active trading, so dropping old frames is free.
CLIENT_QUEUE_MAX = 256

# A transport connection: send text frames, iterate incoming text frames.
Transport = Any
Connector = Callable[[], AsyncIterator[Transport]]


class CandleStreamService:
    def __init__(
        self,
        provider: HyperliquidProvider,
        *,
        connector: Callable[[], Any] | None = None,
    ) -> None:
        self._provider = provider
        if connector is None:
            import websockets

            connector = lambda: websockets.connect(  # noqa: E731
                HL_WS_URL, ping_interval=20, ping_timeout=20
            )
        self._connector = connector
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[str]]] = {}
        self._coins: dict[tuple[str, str], str] = {}
        self._upstream_task: asyncio.Task[None] | None = None
        self._ws: Transport | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def subscribe(
        self, symbol: str, interval: str, asset_type: str
    ) -> asyncio.Queue[str] | None:
        """Attach a client queue; None when the symbol cannot stream."""
        symbol = symbol.strip().upper()
        if not symbol or interval not in STREAM_INTERVALS:
            return None
        coin = await self._provider.candle_coin(symbol, asset_type)
        if coin is None:
            return None
        key = (symbol, interval)
        async with self._lock:
            if self._closed:
                return None
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
            subscribers = self._subscribers.setdefault(key, set())
            subscribers.add(queue)
            first = len(subscribers) == 1
            if first:
                self._coins[key] = coin
                self._ensure_upstream()
            ws = self._ws
        if first and ws is not None:
            await self._send_frame(ws, "subscribe", coin, interval)
        return queue

    async def unsubscribe(
        self, symbol: str, interval: str, queue: asyncio.Queue[str]
    ) -> None:
        key = (symbol.strip().upper(), interval)
        async with self._lock:
            subscribers = self._subscribers.get(key)
            if subscribers is None or queue not in subscribers:
                return
            subscribers.discard(queue)
            if subscribers:
                return
            del self._subscribers[key]
            coin = self._coins.pop(key, None)
            ws = self._ws
            empty = not self._coins
        if coin is not None and ws is not None:
            await self._send_frame(ws, "unsubscribe", coin, interval)
        if empty:
            await self._stop_upstream()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
        await self._stop_upstream()

    # --- upstream ----------------------------------------------------------

    def _ensure_upstream(self) -> None:
        task = self._upstream_task
        if task is not None and not task.done():
            return
        self._upstream_task = asyncio.create_task(
            self._upstream_loop(), name="candle_stream_upstream"
        )
        self._upstream_task.add_done_callback(self._log_upstream_exit)

    async def _stop_upstream(self) -> None:
        task = self._upstream_task
        self._upstream_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._ws = None

    async def _upstream_loop(self) -> None:
        backoff = RECONNECT_BASE_SECONDS
        while not self._closed:
            try:
                async with self._connector() as ws:
                    async with self._lock:
                        self._ws = ws
                        active = list(self._coins.items())
                    for (_symbol, interval), coin in active:
                        await self._send_frame(ws, "subscribe", coin, interval)
                    backoff = RECONNECT_BASE_SECONDS
                    async for raw in ws:
                        self._route(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("candle upstream dropped; reconnecting", exc_info=True)
            finally:
                async with self._lock:
                    self._ws = None
            if not self._coins:
                # Raced with the last unsubscribe: idle instead of reconnecting.
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def _send_frame(
        self, ws: Transport, method: str, coin: str, interval: str
    ) -> None:
        try:
            await ws.send(
                json.dumps(
                    {
                        "method": method,
                        "subscription": {"type": "candle", "coin": coin, "interval": interval},
                    }
                )
            )
        except Exception:
            # A dead connection fails the loop's next recv and reconnects;
            # active coins resubscribe there, so this send is best-effort.
            logger.debug("candle %s frame failed for %s", method, coin)

    def _route(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if message.get("channel") != "candle":
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        coin = str(data.get("s") or "")
        interval = str(data.get("i") or "")
        bar = _candle_bar(data)
        if bar is None:
            return
        for (symbol, sub_interval), sub_coin in self._coins.items():
            if sub_coin != coin or sub_interval != interval:
                continue
            frame = json.dumps(
                {"type": "candle", "symbol": symbol, "interval": interval, "bar": bar},
                separators=(",", ":"),
            )
            for queue in self._subscribers.get((symbol, sub_interval), ()):
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    pass  # slow client: next frame lands in under a minute

    @staticmethod
    def _log_upstream_exit(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("candle upstream loop crashed", exc_info=exc)


def _candle_bar(data: dict[str, Any]) -> dict[str, object] | None:
    """Normalize one HL candle frame; None on malformed values."""
    try:
        timestamp_ms = int(str(data["t"]))
        open_ = float(str(data["o"]))
        high = float(str(data["h"]))
        low = float(str(data["l"]))
        close = float(str(data["c"]))
        volume = float(str(data.get("v") or 0.0))
    except (KeyError, ValueError):
        return None
    if min(open_, high, low, close) <= 0:
        return None
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return {
        "timestamp": timestamp.isoformat(),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
