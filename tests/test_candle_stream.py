"""Candle stream fan-out: refcounts, routing, reconnect resubscription."""

import asyncio
import json
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

import pytest

from app.providers.hyperliquid import HyperliquidProvider
from app.services.candle_stream import CandleStreamService, _candle_bar


class FakeTransport:
    """Scriptable upstream WS: sent frames recorded, incoming frames queued.

    Queueing an Exception instance kills the connection: the next iteration
    raises it, the way a dropped socket fails the real recv.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.active: set[tuple[str, str]] = set()

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))
        frame = self.sent[-1]
        subscription = frame["subscription"]
        key = (subscription["coin"], subscription["interval"])
        if frame["method"] == "subscribe":
            self.active.add(key)
        else:
            self.active.discard(key)

    def kill(self) -> None:
        self.incoming.put_nowait(ConnectionError("dropped"))

    def publish(self, coin: str, interval: str, close: float) -> None:
        """Only subscribed markets can deliver frames, as on the real WS."""
        if (coin, interval) in self.active:
            self.incoming.put_nowait(candle_frame(coin, interval, close))

    def __aiter__(self) -> "FakeTransport":
        return self

    async def __anext__(self) -> str:
        try:
            item = await asyncio.wait_for(self.incoming.get(), timeout=5)
        except TimeoutError:
            raise StopAsyncIteration from None
        if isinstance(item, Exception):
            raise item
        return item


class FakeConnector:
    def __init__(self) -> None:
        self.connections: list[FakeTransport] = []

    @asynccontextmanager
    async def __call__(self) -> Any:
        transport = FakeTransport()
        self.connections.append(transport)
        try:
            yield transport
        finally:
            transport.kill()


def provider_with_markets() -> HyperliquidProvider:
    provider = HyperliquidProvider()
    provider._crypto = {"BTC": {"coin": "BTC", "display": "BTC", "last": 80000.0}}
    provider._tradfi = {"AAPL": {"coin": "xyz:AAPL", "display": "AAPL", "last": 330.0}}
    provider._markets_time = monotonic()
    provider._crypto_time = provider._markets_time
    provider._tradfi_time = provider._markets_time
    return provider


def candle_frame(coin: str, interval: str, close: float, t: int = 1788452160000) -> str:
    return json.dumps(
        {
            "channel": "candle",
            "data": {
                "t": t,
                "T": t + 59999,
                "s": coin,
                "i": interval,
                "o": close - 1,
                "h": close + 1,
                "l": close - 2,
                "c": close,
                "v": "12.5",
                "n": 7,
            },
        }
    )


async def settled() -> None:
    """Let scheduled upstream work run to its next suspension point."""
    for _ in range(10):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_subscribe_rejects_unknown_symbol_and_bad_interval() -> None:
    service = CandleStreamService(provider_with_markets(), connector=FakeConnector())
    assert await service.subscribe("NOPE", "1m", "equity") is None
    assert await service.subscribe("BTC", "3m", "crypto_perp") is None  # not surfaced
    assert service._upstream_task is None


@pytest.mark.asyncio
async def test_subscribe_opens_upstream_and_routes_frames() -> None:
    connector = FakeConnector()
    service = CandleStreamService(provider_with_markets(), connector=connector)

    queue = await service.subscribe("BTC", "1m", "crypto_perp")
    assert queue is not None
    await settled()

    assert len(connector.connections) == 1
    assert connector.connections[0].sent == [
        {"method": "subscribe", "subscription": {"type": "candle", "coin": "BTC", "interval": "1m"}}
    ]

    connector.connections[0].incoming.put_nowait(candle_frame("BTC", "1m", 80123.0))
    frame = json.loads(await asyncio.wait_for(queue.get(), timeout=5))
    assert frame["type"] == "candle"
    assert frame["symbol"] == "BTC"
    assert frame["interval"] == "1m"
    assert frame["bar"]["close"] == 80123.0
    assert frame["bar"]["volume"] == 12.5

    # Frames for other coins/intervals never reach this subscriber.
    connector.connections[0].incoming.put_nowait(candle_frame("xyz:AAPL", "1m", 330.0))
    connector.connections[0].incoming.put_nowait(candle_frame("BTC", "5m", 80200.0))
    assert queue.empty()

    await service.aclose()


@pytest.mark.asyncio
async def test_refcounted_unsubscribe_tears_down_upstream() -> None:
    connector = FakeConnector()
    service = CandleStreamService(provider_with_markets(), connector=connector)

    first = await service.subscribe("AAPL", "1m", "equity")
    second = await service.subscribe("AAPL", "1m", "equity")
    assert first is not None and second is not None and first is not second
    await settled()
    upstream = connector.connections[0]
    assert len(upstream.sent) == 1  # one upstream subscription for two clients

    await service.unsubscribe("AAPL", "1m", first)
    await settled()
    assert len(upstream.sent) == 1  # still one client: no unsubscribe frame

    upstream.incoming.put_nowait(candle_frame("xyz:AAPL", "1m", 331.0))
    assert first.empty()
    frame = json.loads(await asyncio.wait_for(second.get(), timeout=5))
    assert frame["bar"]["close"] == 331.0

    await service.unsubscribe("AAPL", "1m", second)
    await settled()
    assert upstream.sent[-1] == {
        "method": "unsubscribe",
        "subscription": {"type": "candle", "coin": "xyz:AAPL", "interval": "1m"},
    }
    # Last client gone: upstream loop torn down, no idle connection.
    await settled()
    assert service._upstream_task is None

    await service.aclose()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_active_coins() -> None:
    connector = FakeConnector()
    service = CandleStreamService(provider_with_markets(), connector=connector)

    queue = await service.subscribe("BTC", "1m", "crypto_perp")
    assert queue is not None
    await settled()
    first = connector.connections[0]

    # Kill the transport; the loop must reconnect and replay subscriptions.
    first.kill()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(connector.connections) > 1:
            break
    assert len(connector.connections) == 2
    await settled()
    second = connector.connections[1]
    assert second.sent == [
        {"method": "subscribe", "subscription": {"type": "candle", "coin": "BTC", "interval": "1m"}}
    ]

    second.incoming.put_nowait(candle_frame("BTC", "1m", 80555.0))
    frame = json.loads(await asyncio.wait_for(queue.get(), timeout=5))
    assert frame["bar"]["close"] == 80555.0

    await service.aclose()


def test_candle_bar_rejects_malformed_frames() -> None:
    assert _candle_bar({"t": "1", "o": "1", "h": "1", "l": "1", "c": "0", "v": "0"}) is None
    assert _candle_bar({"t": "bad", "o": "1", "h": "1", "l": "1", "c": "1"}) is None
    bar = _candle_bar({"t": 1788452160000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3"})
    assert bar is not None
    assert bar["timestamp"] == "2026-09-03T16:16:00+00:00"
    assert bar["close"] == 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_other_chart", [False, True])
@pytest.mark.parametrize(
    ("symbol", "asset_type", "coin"),
    [("BTC", "crypto_perp", "BTC"), ("AAPL", "equity", "xyz:AAPL")],
)
async def test_subscribe_during_last_unsubscribe_keeps_streaming(
    keep_other_chart: bool, symbol: str, asset_type: str, coin: str
) -> None:
    unsubscribe_started = asyncio.Event()
    allow_unsubscribe = asyncio.Event()

    class GatedTransport(FakeTransport):
        async def send(self, text: str) -> None:
            if json.loads(text)["method"] == "unsubscribe":
                unsubscribe_started.set()
                await allow_unsubscribe.wait()
            await super().send(text)

    connector = FakeConnector()

    @asynccontextmanager
    async def connect() -> Any:
        transport = GatedTransport()
        connector.connections.append(transport)
        try:
            yield transport
        finally:
            transport.kill()

    service = CandleStreamService(provider_with_markets(), connector=connect)
    old = await service.subscribe("BTC", "1m", "crypto_perp")
    assert old is not None
    if keep_other_chart:
        assert await service.subscribe("BTC", "5m", "crypto_perp") is not None
    await settled()
    removing = asyncio.create_task(service.unsubscribe("BTC", "1m", old))
    await asyncio.wait_for(unsubscribe_started.wait(), timeout=1)
    replacing = asyncio.create_task(service.subscribe(symbol, "1m", asset_type))
    # Put the new subscribe inside the upstream unsubscribe's suspension.
    await asyncio.sleep(0)
    allow_unsubscribe.set()
    try:
        await removing
        replacement = await replacing
        assert replacement is not None
        await settled()
        connector.connections[-1].publish(coin, "1m", 333.0)
        frame = json.loads(await asyncio.wait_for(replacement.get(), timeout=1))
        assert frame["symbol"] == symbol
        assert frame["bar"]["close"] == 333.0
    finally:
        allow_unsubscribe.set()
        await asyncio.gather(removing, replacing, return_exceptions=True)
        await service.aclose()


@pytest.mark.asyncio
async def test_old_connection_cleanup_cannot_clear_replacement_connection() -> None:
    closing_started = asyncio.Event()
    allow_close = asyncio.Event()
    connector = FakeConnector()

    @asynccontextmanager
    async def connect() -> Any:
        transport = FakeTransport()
        connector.connections.append(transport)
        first = len(connector.connections) == 1
        try:
            yield transport
        finally:
            if first:
                closing_started.set()
                await allow_close.wait()
            transport.kill()

    service = CandleStreamService(provider_with_markets(), connector=connect)
    old = await service.subscribe("BTC", "1m", "crypto_perp")
    assert old is not None
    await settled()
    removing = asyncio.create_task(service.unsubscribe("BTC", "1m", old))
    await asyncio.wait_for(closing_started.wait(), timeout=1)
    try:
        replacement = await service.subscribe("BTC", "1m", "crypto_perp")
        assert replacement is not None
        await settled()
        allow_close.set()
        await removing
        # This chart uses the replacement transport after the old task's
        # finally block finishes; clearing _ws there would lose this subscribe.
        following = await service.subscribe("AAPL", "1m", "equity")
        assert following is not None
        upstream = connector.connections[-1]
        upstream.publish("BTC", "1m", 80111.0)
        upstream.publish("xyz:AAPL", "1m", 334.0)
        replacement_frame = json.loads(await asyncio.wait_for(replacement.get(), timeout=1))
        following_frame = json.loads(await asyncio.wait_for(following.get(), timeout=1))
        assert replacement_frame["bar"]["close"] == 80111.0
        assert following_frame["bar"]["close"] == 334.0
    finally:
        allow_close.set()
        await asyncio.gather(removing, return_exceptions=True)
        await service.aclose()
