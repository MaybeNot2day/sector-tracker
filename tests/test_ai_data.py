from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app import scheduler
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
        "benchmarks": {"artificial_analysis": {"intelligence_index": 55}},
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
        "frontier_price": 3.0,
        "china_price": None,
        "total_tokens": 600,
        "priced_tokens": 600,
        "coverage_pct": 100.0,
        "open_share_pct": 66.67,
        "model_count": 2,
    }
    methodology = cast(dict[str, Any], payload["methodology"])
    assert "not only Anthropic and OpenAI" in methodology["description"]
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
async def test_token_index_prices_only_chinese_model_creators(tmp_path: Path) -> None:
    models = [
        {
            "id": "qwen/qwen-test",
            "canonical_slug": "qwen/qwen-test",
            "name": "Qwen: Test",
            "architecture": {"modality": "text->text", "output_modalities": ["text"]},
            "pricing": {"prompt": "0.000001", "completion": "0.000003"},
        },
        {
            "id": "openai/gpt-test",
            "canonical_slug": "openai/gpt-test",
            "name": "OpenAI: Test",
            "architecture": {"modality": "text->text", "output_modalities": ["text"]},
            "pricing": {"prompt": "0.000005", "completion": "0.000005"},
        },
    ]
    rankings = [
        {
            "date": "2026-08-18 00:00:00",
            "model_permaslug": model["id"],
            "variant_permaslug": model["id"],
            "total_prompt_tokens": 100,
            "total_completion_tokens": 100,
        }
        for model in models
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": models})
        if request.url.path == "/api/frontend/v1/rankings/models":
            return httpx.Response(200, json={"data": rankings})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIDataService(tmp_path / "board.sqlite3", client=client)

    payload = await service.get_token_index()
    latest = cast(dict[str, Any], payload["latest"])
    methodology = cast(dict[str, Any], payload["methodology"])

    assert latest["index_price"] == 3.5
    assert latest["china_price"] == 2.0
    assert "qwen" in methodology["china_provider_prefixes"]
    assert "openai" not in methodology["china_provider_prefixes"]
    await client.aclose()


@pytest.mark.asyncio
async def test_new_catalog_model_automatically_joins_next_index_refresh(
    tmp_path: Path,
) -> None:
    state: dict[str, list[dict[str, Any]]] = {
        "models": cast(list[dict[str, Any]], list(MODELS)),
        "rankings": list(RANKINGS),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": state["models"]})
        if request.url.path == "/api/frontend/v1/rankings/models":
            return httpx.Response(200, json={"data": state["rankings"]})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIDataService(tmp_path / "board.sqlite3", cache_seconds=0, client=client)
    before = await service.get_token_index()

    state["models"].append(
        {
            "id": "new/model-c",
            "canonical_slug": "new/model-c",
            "name": "New Co: Model C",
            "context_length": 256_000,
            "architecture": {
                "modality": "text->text",
                "output_modalities": ["text"],
            },
            "pricing": {"prompt": "0.000005", "completion": "0.000005"},
        }
    )
    state["rankings"].append(
        {
            "date": "2026-08-18 00:00:00",
            "model_permaslug": "new/model-c",
            "variant_permaslug": "new/model-c",
            "total_prompt_tokens": 300,
            "total_completion_tokens": 300,
        }
    )

    after = await service.get_token_index()
    catalog = await service.get_models()

    before_latest = cast(dict[str, Any], before["latest"])
    after_latest = cast(dict[str, Any], after["latest"])
    catalog_models = cast(list[dict[str, Any]], catalog["models"])
    top_models = cast(list[dict[str, Any]], after["top_models"])

    assert before_latest["model_count"] == 2
    assert after_latest == {
        "date": "2026-08-18",
        "index_price": 3.5,
        "open_price": 1.5,
        "proprietary_price": 4.5,
        "frontier_price": 3.0,
        "china_price": None,
        "total_tokens": 1200,
        "priced_tokens": 1200,
        "coverage_pct": 100.0,
        "open_share_pct": 33.33,
        "model_count": 3,
    }
    assert any(model["id"] == "new/model-c" for model in catalog_models)
    assert any(model["id"] == "new/model-c" for model in top_models)
    await client.aclose()


@pytest.mark.asyncio
async def test_ai_data_warm_loop_refreshes_every_ai_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class AIDataProbe:
        async def get_models(self) -> None:
            calls.append("models")

        async def get_token_index(self) -> None:
            calls.append("token-index")

    class CapexProbe:
        async def get_capex(self) -> None:
            calls.append("capex")

    class GPUComputeProbe:
        async def get_hardware(self) -> None:
            calls.append("hardware")

    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.scheduler.asyncio.sleep", fake_sleep)
    state = SimpleNamespace(
        ai_data_service=AIDataProbe(),
        ai_capex_service=CapexProbe(),
        gpu_compute_service=GPUComputeProbe(),
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler.ai_data_warm_loop(state)

    assert calls == ["models", "token-index", "capex", "hardware"]


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
