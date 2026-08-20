from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app import db
from app.services.gpu_compute import GPUComputeService

API_ROWS: list[dict[str, Any]] = [
    {
        "provider": "Lambda",
        "provider_slug": "lambda",
        "gpu": "H100 80GB",
        "gpu_slug": "h100",
        "vram_gb": 80,
        "architecture": "Hopper",
        "gpu_count": 1,
        "price_per_hour_usd": 2.49,
        "total_hourly_usd": 2.49,
        "pricing_type": "on_demand",
        "commitment_months": None,
        "source_url": "https://lambda.test/pricing",
        "last_updated": "2026-08-19T10:15:30Z",
    },
    {
        "provider": "Runpod",
        "provider_slug": "runpod",
        "gpu": "H100 80GB",
        "gpu_slug": "h100",
        "vram_gb": 80,
        "architecture": "Hopper",
        "gpu_count": 8,
        "price_per_hour_usd": 2.19,
        "total_hourly_usd": 17.52,
        "pricing_type": "spot",
        "commitment_months": None,
        "source_url": "https://runpod.test/pricing",
        "last_updated": "2026-08-19T10:15:30Z",
    },
]


@pytest.mark.asyncio
async def test_authenticated_api_normalizes_and_persists_gpu_prices(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer cp_live_test"
        return httpx.Response(
            200,
            json={
                "data": API_ROWS,
                "meta": {
                    "generated_at": "2026-08-19T12:00:00Z",
                    "tier": "free",
                },
            },
        )

    database = tmp_path / "board.sqlite3"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GPUComputeService(database, api_key="cp_live_test", cache_seconds=0, client=client)

    payload = await service.get_hardware()

    assert payload["as_of"] == "2026-08-19T12:00:00Z"
    source = cast(dict[str, Any], payload["source"])
    assert source["mode"] == "authenticated_api"
    assert payload["summary"] == {
        "models": 1,
        "detailed_models": 1,
        "offers": 2,
        "providers": 2,
        "lowest_price": 2.19,
    }
    model = payload["models"][0]  # type: ignore[index]
    assert model["name"] == "H100 80GB"
    assert model["min_price"] == 2.19
    assert model["max_price"] == 2.49
    assert [offer["provider"] for offer in model["offers"]] == ["Runpod", "Lambda"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_gpu_compute_snapshots").fetchone() == (1,)

    await client.aclose()


@pytest.mark.asyncio
async def test_public_pages_supply_model_summary_provider_offers_and_stale_fallback(
    tmp_path: Path,
) -> None:
    failing = False
    catalog = """
    <table><tbody><tr>
      <td></td><td><a href="/gpus/h100">H100 SXM</a></td><td>80 GB</td>
      <td>$3.65/hr</td><td>$1.66 – $11.06</td><td>42</td>
    </tr></tbody></table>
    """
    detail = """
    <table><tbody>
      <tr><td><a href="/providers/verda">Verda</a></td><td>$1.63/hr 1×</td>
      <td>1×</td><td>8/20/2026</td><td><a href="https://verda.test/pricing">Source</a></td></tr>
      <tr><td><a href="/providers/civo">Civo</a></td><td>$2.49/hr36mo 8×</td>
      <td>8×</td><td>8/19/2026</td><td><a href="https://civo.test/pricing">Source</a></td></tr>
    </tbody></table>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if failing:
            return httpx.Response(503)
        if request.url.path == "/gpu":
            return httpx.Response(200, text=catalog)
        if request.url.path == "/gpus/h100":
            return httpx.Response(200, text=detail)
        return httpx.Response(404)

    database = tmp_path / "board.sqlite3"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GPUComputeService(database, cache_seconds=0, client=client)

    payload = await service.get_hardware()
    model = payload["models"][0]  # type: ignore[index]
    assert payload["as_of"] == "2026-08-20"
    assert payload["source"]["mode"] == "public_page"  # type: ignore[index]
    assert model["provider_count"] == 42
    assert model["vram_gb"] == 80
    assert [offer["provider"] for offer in model["offers"]] == ["Verda", "Civo"]
    assert model["offers"][1]["pricing_type"] == "reserved"
    assert model["offers"][1]["commitment_months"] == 36

    failing = True
    stale = await service.get_hardware()
    assert stale["stale"] is True
    assert "public page" in str(stale["warning"])
    assert db.load_latest_ai_gpu_compute_snapshot(database) is not None

    await client.aclose()
