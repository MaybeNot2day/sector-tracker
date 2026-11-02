from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app import db, scheduler
from app.services import ai_data
from app.services.ai_data import MIN_INDEX_MODELS, AIDataError, AIDataService

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


def _text_model(model_id: str, prompt: str, completion: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": model_id,
        "canonical_slug": model_id,
        "name": f"{model_id.partition('/')[0]}: {model_id}",
        "architecture": {"modality": "text->text", "output_modalities": ["text"]},
        "pricing": {"prompt": prompt, "completion": completion},
        **extra,
    }


def _ranking_row(
    day: str, slug: str, prompt: int, completion: int, variant: str = ""
) -> dict[str, Any]:
    return {
        "date": f"{day} 00:00:00",
        "model_permaslug": slug,
        "variant": variant or "standard",
        "variant_permaslug": f"{slug}:{variant}" if variant else slug,
        "total_prompt_tokens": prompt,
        "total_completion_tokens": completion,
    }


def _handler(models: list[dict[str, Any]], rankings: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": models})
        if request.url.path == "/api/frontend/v1/rankings/models":
            return httpx.Response(200, json={"data": rankings})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def small_index_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price-math tests use a handful of models; completeness has its own tests."""
    monkeypatch.setattr(ai_data, "MIN_INDEX_MODELS", 1)


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
async def test_token_index_prices_observed_mix_and_persists_history(
    tmp_path: Path, small_index_threshold: None
) -> None:
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
async def test_token_index_prices_only_chinese_model_creators(
    tmp_path: Path, small_index_threshold: None
) -> None:
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
    small_index_threshold: None,
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

    class NeocloudProbe:
        async def get_financing(self) -> None:
            calls.append("neoclouds")

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
        neocloud_finance_service=NeocloudProbe(),
        gpu_compute_service=GPUComputeProbe(),
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler.ai_data_warm_loop(state)

    assert calls == ["models", "token-index", "capex", "neoclouds", "hardware"]


@pytest.mark.asyncio
async def test_service_serves_stale_cache_with_real_as_of_and_backs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(ai_data, "monotonic", lambda: clock[0])
    state: dict[str, Any] = {"failing": False, "requests": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"] += 1
        if state["failing"]:
            return httpx.Response(503)
        return _transport(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIDataService(tmp_path / "board.sqlite3", cache_seconds=0, client=client)
    first = await service.get_models()
    state["failing"] = True
    clock[0] += 1

    stale = await service.get_models()
    requests_after_failure = state["requests"]
    clock[0] += 1
    during_cooldown = await service.get_models()

    assert first["stale"] is False
    assert stale["summary"] == first["summary"]
    assert stale["stale"] is True
    assert stale["as_of"] == first["as_of"]
    assert during_cooldown["stale"] is True
    assert state["requests"] == requests_after_failure

    state["failing"] = False
    clock[0] += ai_data.FAILURE_RETRY_SECONDS
    recovered = await service.get_models()

    assert recovered["stale"] is False
    assert state["requests"] == requests_after_failure + 1
    await client.aclose()


@pytest.mark.asyncio
async def test_service_without_cache_fails_fast_during_cooldown(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AIDataService(tmp_path / "board.sqlite3", client=client)

    for _ in range(3):
        with pytest.raises(AIDataError):
            await service.get_models()

    assert requests == 1
    await client.aclose()


def _index_point(
    day: str, *, index_price: float, model_count: int, total_tokens: int
) -> dict[str, Any]:
    return {
        "date": day,
        "index_price": index_price,
        "open_price": None,
        "proprietary_price": None,
        "frontier_price": None,
        "china_price": None,
        "total_tokens": total_tokens,
        "priced_tokens": total_tokens,
        "coverage_pct": 100.0,
        "open_share_pct": 0.0,
        "model_count": model_count,
    }


@pytest.mark.asyncio
async def test_token_index_stores_only_complete_days_and_keeps_stored_complete_day(
    tmp_path: Path,
) -> None:
    # Shape of OpenRouter's week view: every model for the newest day, a few
    # leftover rows for older days (live: 648 rows for 10-29, 1-11 per earlier day).
    model_ids = [f"lab/model-{index:03d}" for index in range(MIN_INDEX_MODELS + 20)]
    models = [_text_model(model_id, "0.000001", "0.000001") for model_id in model_ids]
    rankings = [_ranking_row("2026-10-29", model_id, 1_000, 1_000) for model_id in model_ids]
    rankings += [_ranking_row("2026-10-28", model_id, 50, 50) for model_id in model_ids[:3]]
    rankings += [_ranking_row("2026-10-27", model_ids[0], 10, 10)]
    database = tmp_path / "board.sqlite3"
    complete_yesterday = _index_point(
        "2026-10-28", index_price=0.43, model_count=345, total_tokens=180_000_000_000_000
    )
    db.save_ai_token_index_points(database, [complete_yesterday])

    client = httpx.AsyncClient(transport=_handler(models, rankings))
    service = AIDataService(database, client=client)
    payload = await service.get_token_index()

    series = cast(list[dict[str, Any]], payload["series"])
    assert [point["date"] for point in series] == ["2026-10-28", "2026-10-29"]
    assert series[0] == complete_yesterday
    assert series[1]["model_count"] == len(model_ids)
    assert cast(dict[str, Any], payload["latest"])["date"] == "2026-10-29"
    stored = db.load_ai_token_index_points(database, 365)
    assert stored == series
    await client.aclose()


def test_token_index_upsert_never_replaces_fuller_stored_day(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    complete = _index_point("2026-10-28", index_price=0.43, model_count=345, total_tokens=10**14)
    thin = _index_point("2026-10-28", index_price=19.6, model_count=3, total_tokens=10**9)
    fuller = _index_point("2026-10-28", index_price=0.45, model_count=350, total_tokens=2 * 10**14)

    db.save_ai_token_index_points(database, [complete])
    db.save_ai_token_index_points(database, [thin])
    assert db.load_ai_token_index_points(database, 10) == [complete]

    db.save_ai_token_index_points(database, [fuller])
    assert db.load_ai_token_index_points(database, 10) == [fuller]


@pytest.mark.asyncio
async def test_empty_hugging_face_id_is_proprietary_and_can_be_frontier(
    tmp_path: Path, small_index_threshold: None
) -> None:
    # Live OpenRouter sends hugging_face_id "" for proprietary flagships.
    gpt = _text_model(
        "openai/gpt-5.5",
        "0.000005",
        "0.00003",
        hugging_face_id="",
        benchmarks={"artificial_analysis": {"intelligence_index": 70}},
    )
    open_model = _text_model("open/model-a", "0.000001", "0.000001", hugging_face_id="open/model-a")
    rankings = [
        _ranking_row("2026-10-29", "openai/gpt-5.5", 1_000, 1_000),
        _ranking_row("2026-10-29", "open/model-a", 1_000, 1_000),
    ]
    client = httpx.AsyncClient(transport=_handler([gpt, open_model], rankings))
    service = AIDataService(tmp_path / "board.sqlite3", client=client)

    catalog = await service.get_models()
    index = await service.get_token_index()

    flags = {m["id"]: m["is_open_weight"] for m in cast(list[dict[str, Any]], catalog["models"])}
    assert flags == {"openai/gpt-5.5": False, "open/model-a": True}
    latest = cast(dict[str, Any], index["latest"])
    assert latest["frontier_price"] == 17.5
    assert latest["open_share_pct"] == 50.0
    await client.aclose()


@pytest.mark.asyncio
async def test_batch_variant_priced_at_batch_rate_and_counted_once(
    tmp_path: Path, small_index_threshold: None
) -> None:
    # Real catalog: both entries share the dated canonical slug; rankings name
    # the batch traffic by "<dated slug>:batch".
    dated = "openai/gpt-5-mini-2025-08-07"
    standard = {
        **_text_model("openai/gpt-5-mini", "0.00000025", "0.000002"),
        "canonical_slug": dated,
    }
    batch = {
        **_text_model("openai/gpt-5-mini:batch", "0.000000125", "0.000001"),
        "canonical_slug": dated,
    }
    rankings = [
        _ranking_row("2026-10-29", dated, 1_000_000, 1_000_000),
        _ranking_row("2026-10-29", dated, 1_000_000, 1_000_000, variant="batch"),
    ]
    client = httpx.AsyncClient(transport=_handler([standard, batch], rankings))
    service = AIDataService(tmp_path / "board.sqlite3", client=client)

    payload = await service.get_token_index()

    latest = cast(dict[str, Any], payload["latest"])
    # (0.25 + 2.0 standard + 0.125 + 1.0 batch) USD over 4M tokens.
    assert latest["index_price"] == 0.8438
    assert latest["model_count"] == 1
    top_models = cast(list[dict[str, Any]], payload["top_models"])
    assert [(m["id"], m["tokens"], m["effective_price"]) for m in top_models] == [
        ("openai/gpt-5-mini", 4_000_000, 0.8438)
    ]
    await client.aclose()
