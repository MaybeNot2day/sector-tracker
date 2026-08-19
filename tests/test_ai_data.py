from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.services.ai_data import AIDataService

MODELS = [
    {
        "id": "open/model-a",
        "canonical_slug": "open/model-a",
        "name": "Open Labs: Model A",
        "hugging_face_id": "open/model-a",
        "context_length": 128_000,
        "architecture": {
            "modality": "text->text",
            "output_modalities": ["text"],
        },
        "pricing": {
            "prompt": "0.000001",
            "completion": "0.000003",
            "input_cache_read": "0.0000002",
        },
        "benchmarks": {"artificial_analysis": {"intelligence_index": 42}},
    },
    {
        "id": "closed/model-b",
        "canonical_slug": "closed/model-b",
        "name": "Closed Inc: Model B",
        "context_length": 64_000,
        "architecture": {
            "modality": "text->text",
            "output_modalities": ["text"],
        },
        "pricing": {"prompt": "0.000002", "completion": "0.000004"},
    },
    {
        "id": "image/only",
        "canonical_slug": "image/only",
        "name": "Image: Only",
        "architecture": {
            "modality": "text->image",
            "output_modalities": ["image"],
        },
        "pricing": {"prompt": "0.000001", "completion": "0.000001"},
    },
]

RANKINGS = [
    {
        "date": "2026-08-17 00:00:00",
        "model_permaslug": "open/model-a",
        "variant_permaslug": "open/model-a",
        "total_prompt_tokens": 100,
        "total_completion_tokens": 100,
    },
    {
        "date": "2026-08-18 00:00:00",
        "model_permaslug": "open/model-a",
        "variant_permaslug": "open/model-a",
        "total_prompt_tokens": 300,
        "total_completion_tokens": 100,
    },
    {
        "date": "2026-08-18 00:00:00",
        "model_permaslug": "closed/model-b",
        "variant_permaslug": "closed/model-b",
        "total_prompt_tokens": 100,
        "total_completion_tokens": 100,
    },
]


def _transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/models":
        return httpx.Response(200, json={"data": MODELS})
    if request.url.path == "/api/frontend/v1/rankings/models":
        return httpx.Response(200, json={"data": RANKINGS})
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_model_catalog_normalizes_prices_and_persists_snapshot(tmp_path: Path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_transport))
    service = AIDataService(tmp_path / "board.sqlite3", client=client)

    payload = await service.get_models()

    assert payload["summary"] == {
        "models": 2,
        "providers": 2,
        "open_weight": 1,
        "free": 0,
        "median_blended_price": 2.0,
    }
    model_rows = cast(list[dict[str, Any]], payload["models"])
    models = {model["id"]: model for model in model_rows}
    assert models["open/model-a"]["input_price_per_million"] == 1.0
    assert models["open/model-a"]["output_price_per_million"] == 3.0
    assert models["open/model-a"]["blended_price_per_million"] == 1.5
    assert models["open/model-a"]["is_open_weight"] is True
    assert models["closed/model-b"]["blended_price_per_million"] == 2.5

    with sqlite3.connect(tmp_path / "board.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_model_snapshots").fetchone() == (2,)

    await client.aclose()


@pytest.mark.asyncio
async def test_token_index_prices_observed_mix_and_persists_history(tmp_path: Path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_transport))
    service = AIDataService(tmp_path / "board.sqlite3", client=client)

    payload = await service.get_token_index()

    assert payload["as_of"] == "2026-08-18"
    assert payload["latest"] == {
        "date": "2026-08-18",
        "index_price": 2.0,
        "open_price": 1.5,
        "proprietary_price": 3.0,
        "total_tokens": 600,
        "priced_tokens": 600,
        "coverage_pct": 100.0,
        "open_share_pct": 66.67,
        "model_count": 2,
    }
    series = payload["series"]
    assert isinstance(series, list)
    assert [point["date"] for point in series] == ["2026-08-17", "2026-08-18"]
    top_models = payload["top_models"]
    assert isinstance(top_models, list)
    assert top_models[0]["id"] == "open/model-a"
    assert top_models[0]["usage_share_pct"] == 66.67

    with sqlite3.connect(tmp_path / "board.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_token_index").fetchone() == (2,)

    await client.aclose()


@pytest.mark.asyncio
async def test_service_uses_stale_cache_when_upstream_fails(tmp_path: Path) -> None:
    state: dict[str, Any] = {"failing": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["failing"]:
            return httpx.Response(503)
        return _transport(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIDataService(tmp_path / "board.sqlite3", cache_seconds=0, client=client)
    first = await service.get_models()
    state["failing"] = True

    stale = await service.get_models()

    assert stale["summary"] == first["summary"]
    await client.aclose()
