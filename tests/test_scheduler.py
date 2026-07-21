import asyncio
from threading import Event, Lock
from typing import Any

import pytest

from app import scheduler


@pytest.fixture(autouse=True)
def reset_payload_cache() -> None:
    scheduler._payload_cache = None
    scheduler._payload_tasks.clear()
    scheduler._payload_generation = 0


@pytest.mark.asyncio
async def test_board_payload_async_shares_one_inflight_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()
    payload: dict[str, object] = {"groups": []}

    def blocked_build(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(2)
        return payload

    monkeypatch.setattr(scheduler, "_board_payload", blocked_build)
    grouped: dict[str, object] = {}
    callers = [
        asyncio.create_task(scheduler.board_payload_async(object(), [], grouped))
        for _ in range(8)
    ]

    assert await asyncio.to_thread(entered.wait, 2)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*callers)

    assert calls == 1
    assert all(result is payload for result in results)
    assert await scheduler.board_payload_async(object(), [], grouped) is payload
    assert calls == 1


@pytest.mark.asyncio
async def test_board_payload_caller_cancellation_does_not_cancel_shared_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    payload: dict[str, object] = {"overview": {}}

    def blocked_build(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
        entered.set()
        assert release.wait(2)
        return payload

    monkeypatch.setattr(scheduler, "_board_payload", blocked_build)
    grouped: dict[str, object] = {}
    cancelled = asyncio.create_task(scheduler.board_payload_async(object(), [], grouped))
    survivor = asyncio.create_task(scheduler.board_payload_async(object(), [], grouped))

    assert await asyncio.to_thread(entered.wait, 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await survivor is payload
    assert scheduler._payload_cache == (grouped, payload)


@pytest.mark.asyncio
async def test_older_payload_build_cannot_overwrite_newer_finished_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_entered = Event()
    new_entered = Event()
    release_old = Event()
    release_new = Event()
    old_grouped: dict[str, object] = {"snapshot": "old"}
    new_grouped: dict[str, object] = {"snapshot": "new"}
    old_payload: dict[str, object] = {"snapshot": "old"}
    new_payload: dict[str, object] = {"snapshot": "new"}

    def ordered_build(app_state: Any, groups: Any, grouped: Any) -> dict[str, object]:
        if grouped is old_grouped:
            old_entered.set()
            assert release_old.wait(2)
            return old_payload
        new_entered.set()
        assert release_new.wait(2)
        return new_payload

    monkeypatch.setattr(scheduler, "_board_payload", ordered_build)
    old_task = asyncio.create_task(
        scheduler.board_payload_async(object(), [], old_grouped)
    )
    assert await asyncio.to_thread(old_entered.wait, 2)
    new_task = asyncio.create_task(
        scheduler.board_payload_async(object(), [], new_grouped)
    )
    assert await asyncio.to_thread(new_entered.wait, 2)

    release_new.set()
    assert await new_task is new_payload
    release_old.set()
    assert await old_task is old_payload

    assert scheduler._payload_cache == (new_grouped, new_payload)
