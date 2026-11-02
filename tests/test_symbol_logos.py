from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app import main as main_module
from app.services import symbol_logos

_PNG = b"\x89PNG fake logo bytes"


@pytest.fixture(autouse=True)
def _clear_logo_cache() -> None:
    symbol_logos._logo_cache.clear()


@pytest.mark.asyncio
async def test_fetch_symbol_logo_caches_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_upstream(url: str) -> tuple[bytes, str] | None:
        calls.append(url)
        return _PNG, "image/png"

    monkeypatch.setattr(symbol_logos, "_fetch_upstream", fake_upstream)

    first = await symbol_logos.fetch_symbol_logo("AAPL", "stock")
    second = await symbol_logos.fetch_symbol_logo("AAPL", "stock")

    assert first == (_PNG, "image/png")
    assert second == first
    assert len(calls) == 1
    assert "AAPL" in calls[0]


@pytest.mark.asyncio
async def test_fetch_symbol_logo_negative_caches_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_upstream(url: str) -> tuple[bytes, str] | None:
        calls.append(url)
        return None

    monkeypatch.setattr(symbol_logos, "_fetch_upstream", fake_upstream)

    assert await symbol_logos.fetch_symbol_logo("NOPE", "crypto") is None
    assert await symbol_logos.fetch_symbol_logo("NOPE", "crypto") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fetch_symbol_logo_serves_stale_hit_through_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_upstream(url: str) -> tuple[bytes, str] | None:
        return _PNG, "image/svg+xml"

    monkeypatch.setattr(symbol_logos, "_fetch_upstream", fake_upstream)
    assert await symbol_logos.fetch_symbol_logo("BTC", "crypto") is not None
    # Expire the hit, then break the upstream: the stale logo must survive.
    key = "crypto:BTC"
    fetched_at, body, content_type, ttl = symbol_logos._logo_cache[key]
    symbol_logos._logo_cache[key] = (fetched_at - 10 * 24 * 3600.0, body, content_type, ttl)

    async def broken_upstream(url: str) -> tuple[bytes, str] | None:
        return None

    monkeypatch.setattr(symbol_logos, "_fetch_upstream", broken_upstream)

    assert await symbol_logos.fetch_symbol_logo("BTC", "crypto") == (_PNG, "image/svg+xml")


def _upstream_status(monkeypatch: pytest.MonkeyPatch, status: int) -> list[str]:
    calls: list[str] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=b"busy")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_transient_upstream_failure_is_retried_soon(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(symbol_logos, "monotonic", lambda: clock[0])
    calls = _upstream_status(monkeypatch, status)

    assert await symbol_logos.fetch_symbol_logo("NVDA", "stock") is None
    clock[0] += 5 * 60.0  # minutes later, far inside the 6h definitive-miss window
    assert await symbol_logos.fetch_symbol_logo("NVDA", "stock") is None

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_definitive_not_found_stays_negative_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(symbol_logos, "monotonic", lambda: clock[0])
    calls = _upstream_status(monkeypatch, 404)

    assert await symbol_logos.fetch_symbol_logo("NOPE", "stock") is None
    clock[0] += 5 * 60.0
    assert await symbol_logos.fetch_symbol_logo("NOPE", "stock") is None

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_symbol_logo_endpoint_serves_and_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_logo(symbol: str, kind: str) -> tuple[bytes, str] | None:
        return (_PNG, "image/png") if symbol == "AAPL" else None

    monkeypatch.setattr(main_module, "fetch_symbol_logo", fake_logo)

    served = await main_module.symbol_logo("aapl", kind="stock")
    assert served.status_code == 200
    assert served.body == _PNG
    assert served.headers["cache-control"] == "public, max-age=86400"

    missing = await main_module.symbol_logo("ZZZZ", kind="stock")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "public, max-age=21600"

    with pytest.raises(HTTPException) as excinfo:
        await main_module.symbol_logo("AAPL", kind="anything")
    assert excinfo.value.status_code == 422

    with pytest.raises(HTTPException) as excinfo:
        await main_module.symbol_logo("CL=F", kind="stock")
    assert excinfo.value.status_code == 422
