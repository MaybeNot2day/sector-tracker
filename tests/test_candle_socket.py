from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from fastapi import WebSocket

from app import main
from app.providers.hyperliquid import HyperliquidProvider
from app.services.candle_stream import CandleStreamService


@pytest.mark.asyncio
async def test_failed_candle_send_releases_subscription_and_receive_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def candle_coin(self, symbol: str, asset_type: str) -> str:
            return "BTC"

    class Transport:
        def __init__(self) -> None:
            self.sent_first = False

        async def send(self, frame: str) -> None:
            pass

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            if self.sent_first:
                await asyncio.Event().wait()
            self.sent_first = True
            return json.dumps({
                "channel": "candle", "data": {
                    "s": "BTC", "i": "1m", "t": 1788452160000,
                    "o": "100", "h": "102", "l": "99", "c": "101", "v": "1",
                },
            })

    @asynccontextmanager
    async def connector() -> Any:
        yield Transport()

    class Socket:
        receive_cancelled = False
        closed = False

        async def accept(self) -> None:
            pass

        async def send_text(self, frame: str) -> None:
            raise ConnectionError("client cannot receive")

        async def receive_text(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.receive_cancelled = True
                raise
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.closed = True

    service = CandleStreamService(cast(HyperliquidProvider, Provider()), connector=connector)
    monkeypatch.setattr(main.app.state, "candle_stream_service", service, raising=False)
    socket = Socket()
    try:
        await asyncio.wait_for(main.candles_ws(cast(WebSocket, socket), "BTC", "1m"), timeout=1)
        assert socket.receive_cancelled
        assert socket.closed
        assert not service._subscribers
        assert service._upstream_task is None
    finally:
        await service.aclose()
