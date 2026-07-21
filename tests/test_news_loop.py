import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app import scheduler


class StubNewsService:
    """Scripted refresh() results; an Exception entry is raised instead."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.payload = {"items": [{"id": "chan/1"}], "channels": ["chan"]}
        self.payload_calls = 0

    async def refresh(self) -> int:
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return cast(int, result)

    def feed_payload(self) -> dict[str, object]:
        self.payload_calls += 1
        return self.payload


class StubConnectionManager:
    def __init__(self) -> None:
        self.broadcasts: list[dict[str, Any]] = []

    async def broadcast(self, payload: dict[str, Any]) -> None:
        self.broadcasts.append(payload)


async def run_loop(
    news_service: StubNewsService,
    max_sleeps: int,
    monkeypatch: pytest.MonkeyPatch,
) -> StubConnectionManager:
    """Drive news_poll_loop until the Nth asyncio.sleep, then cancel it.

    The loop sleeps once before the first iteration, so max_sleeps=3 runs
    exactly two refresh iterations.
    """
    manager = StubConnectionManager()
    state = SimpleNamespace(
        news_service=news_service,
        connection_manager=manager,
        settings=SimpleNamespace(news_poll_seconds=15),
    )
    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= max_sleeps:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.news_poll_loop(state)
    return manager


@pytest.mark.asyncio
async def test_broadcasts_only_when_refresh_finds_new_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StubNewsService([3, 0])

    manager = await run_loop(service, max_sleeps=3, monkeypatch=monkeypatch)

    assert manager.broadcasts == [{"type": "news", "data": service.payload}]
    assert service.payload_calls == 1  # not built on the empty iteration


@pytest.mark.asyncio
async def test_loop_survives_refresh_exception_and_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StubNewsService([RuntimeError("telegram down"), 5])

    manager = await run_loop(service, max_sleeps=3, monkeypatch=monkeypatch)

    # The raising iteration is swallowed; the next one still broadcasts.
    assert [payload["type"] for payload in manager.broadcasts] == ["news"]
    assert service.payload_calls == 1
