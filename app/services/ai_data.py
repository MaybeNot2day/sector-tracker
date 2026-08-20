"""Free AI market data composed from OpenRouter's public catalog and rankings."""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any

import httpx

from app import db

MODELS_URL = "https://openrouter.ai/api/v1/models"
RANKINGS_URL = "https://openrouter.ai/api/frontend/v1/rankings/models?view=week"
REQUEST_TIMEOUT_SECONDS = 20.0
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "SectorTracker/0.1 (+AI market dashboard)",
}


class AIDataError(RuntimeError):
    """The upstream AI dataset is unavailable or malformed."""


class AIDataService:
    """Fetch, normalize, cache, and persist the first two AI datasets.

    The model catalog is a documented OpenRouter endpoint. Rankings come from
    the public endpoint that powers openrouter.ai/rankings; it is keyless but
    not a versioned API, so parsing is deliberately strict and stale cache is
    retained when the upstream shape changes.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        cache_seconds: float = 15 * 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.database_path = database_path
        self.cache_seconds = cache_seconds
        self._client = client
        self._owns_client = client is None
        self._models_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._rankings_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._models_lock = asyncio.Lock()
        self._rankings_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def get_models(self) -> dict[str, object]:
        raw_models = await self._get_models_raw()
        models = [model for raw in raw_models if (model := _normalize_model(raw)) is not None]
        models.sort(key=lambda item: (str(item["provider"]), str(item["name"])))

        observed_date = datetime.now(UTC).date().isoformat()
        await asyncio.to_thread(
            db.save_ai_model_snapshots,
            self.database_path,
            observed_date,
            models,
        )

        paid_prices = [
            price
            for item in models
            if (price := _finite_number(item.get("blended_price_per_million"))) is not None
            and price > 0
        ]
        return {
            "as_of": datetime.now(UTC).isoformat(),
            "source": {
                "name": "OpenRouter",
                "url": MODELS_URL,
            },
            "summary": {
                "models": len(models),
                "providers": len({str(item["provider"]) for item in models}),
                "open_weight": sum(bool(item["is_open_weight"]) for item in models),
                "free": sum(bool(item["is_free"]) for item in models),
                "median_blended_price": round(median(paid_prices), 4) if paid_prices else None,
            },
            "models": models,
        }

    async def get_token_index(self) -> dict[str, object]:
        raw_models, rankings = await asyncio.gather(
            self._get_models_raw(),
            self._get_rankings_raw(),
        )
        normalized = [
            (raw, model) for raw in raw_models if (model := _normalize_model(raw)) is not None
        ]
        models_by_key: dict[str, dict[str, object]] = {}
        for raw, model in normalized:
            for key in (raw.get("canonical_slug"), raw.get("id")):
                if isinstance(key, str) and key:
                    models_by_key[key] = model

        aggregates: dict[str, dict[str, Any]] = defaultdict(_empty_index_bucket)
        detail_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
        for raw in rankings:
            day = str(raw.get("date") or "")[:10]
            if len(day) != 10:
                continue
            prompt_tokens = _nonnegative_number(raw.get("total_prompt_tokens"))
            completion_tokens = _nonnegative_number(raw.get("total_completion_tokens"))
            if prompt_tokens is None or completion_tokens is None:
                continue
            tokens = prompt_tokens + completion_tokens
            if tokens <= 0:
                continue

            bucket = aggregates[day]
            bucket["total_tokens"] += tokens
            model = _ranking_model(raw, models_by_key)
            if model is None:
                continue
            input_price = _finite_number(model.get("input_price_per_token"))
            output_price = _finite_number(model.get("output_price_per_token"))
            if input_price is None or output_price is None:
                continue

            cost = prompt_tokens * input_price + completion_tokens * output_price
            bucket["priced_tokens"] += tokens
            bucket["cost_usd"] += cost
            bucket["model_ids"].add(str(model["id"]))
            segment = "open" if bool(model["is_open_weight"]) else "proprietary"
            bucket[f"{segment}_tokens"] += tokens
            bucket[f"{segment}_cost_usd"] += cost
            detail_rows[day].append(
                {
                    "id": model["id"],
                    "name": model["name"],
                    "provider": model["provider"],
                    "is_open_weight": model["is_open_weight"],
                    "tokens": int(tokens),
                    "cost_usd": cost,
                    "effective_price": round(cost / tokens * 1_000_000, 4),
                }
            )

        live_series: list[dict[str, object]] = []
        for day in sorted(aggregates):
            point = _index_point(day, aggregates[day])
            if point is not None:
                live_series.append(point)
        if not live_series:
            raise AIDataError("token_index_empty")

        await asyncio.to_thread(db.save_ai_token_index_points, self.database_path, live_series)
        stored_series = await asyncio.to_thread(
            db.load_ai_token_index_points,
            self.database_path,
            365,
        )
        series_by_date = {str(point["date"]): point for point in stored_series}
        series_by_date.update({str(point["date"]): point for point in live_series})
        series = [series_by_date[day] for day in sorted(series_by_date)]

        latest_live = live_series[-1]
        latest_date = str(latest_live["date"])
        latest_details = detail_rows.get(latest_date, [])
        priced_tokens = _finite_number(latest_live["priced_tokens"]) or 0.0
        total_cost = sum(_finite_number(item.get("cost_usd")) or 0.0 for item in latest_details)
        for item in latest_details:
            tokens = _finite_number(item.get("tokens")) or 0.0
            cost = _finite_number(item.get("cost_usd")) or 0.0
            item["usage_share_pct"] = round(tokens / priced_tokens * 100, 2)
            item["cost_share_pct"] = round(cost / total_cost * 100, 2) if total_cost > 0 else 0.0
            item.pop("cost_usd", None)
        latest_details.sort(
            key=lambda item: _finite_number(item.get("tokens")) or 0.0,
            reverse=True,
        )

        return {
            "as_of": latest_date,
            "generated_at": datetime.now(UTC).isoformat(),
            "source": {
                "name": "OpenRouter Rankings",
                "url": "https://openrouter.ai/rankings",
                "attribution": f"Source: OpenRouter (openrouter.ai/rankings), as of {latest_date}.",
            },
            "methodology": {
                "label": "OpenRouter usage-weighted token price",
                "description": (
                    "Observed prompt and completion tokens are priced separately, then "
                    "divided by matched tokens. Token counts use each upstream provider's "
                    "tokenizer and are not standardized across providers. Open-weight is a "
                    "proxy based on a published Hugging Face ID."
                ),
                "formula": (
                    "sum(prompt_tokens * input_price + completion_tokens * output_price) "
                    "/ sum(prompt_tokens + completion_tokens) * 1,000,000"
                ),
                "constituent_rule": (
                    "Every ranked model with an exact current OpenRouter catalog slug and "
                    "text pricing joins automatically on refresh."
                ),
                "open_weight_proxy": True,
            },
            "latest": latest_live,
            "series": series,
            "top_models": latest_details[:15],
        }

    async def _get_models_raw(self) -> list[dict[str, Any]]:
        return await self._get_cached(MODELS_URL, "models", self._models_lock)

    async def _get_rankings_raw(self) -> list[dict[str, Any]]:
        return await self._get_cached(RANKINGS_URL, "rankings", self._rankings_lock)

    async def _get_cached(
        self,
        url: str,
        kind: str,
        lock: asyncio.Lock,
    ) -> list[dict[str, Any]]:
        cache = self._models_cache if kind == "models" else self._rankings_cache
        now = monotonic()
        if cache is not None and now - cache[0] < self.cache_seconds:
            return cache[1]

        async with lock:
            cache = self._models_cache if kind == "models" else self._rankings_cache
            now = monotonic()
            if cache is not None and now - cache[0] < self.cache_seconds:
                return cache[1]
            try:
                response = await self._http_client().get(url, headers=_HEADERS)
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise AIDataError(f"{kind}_payload_invalid")
                typed_rows = list(rows)
            except (httpx.HTTPError, ValueError, AIDataError) as exc:
                if cache is not None:
                    return cache[1]
                raise AIDataError(f"{kind}_unavailable") from exc

            next_cache = (monotonic(), typed_rows)
            if kind == "models":
                self._models_cache = next_cache
            else:
                self._rankings_cache = next_cache
            return typed_rows

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
            self._owns_client = True
        return self._client


def _normalize_model(raw: dict[str, Any]) -> dict[str, object] | None:
    model_id = raw.get("id")
    name = raw.get("name")
    architecture = raw.get("architecture")
    pricing = raw.get("pricing")
    if not isinstance(model_id, str) or not isinstance(name, str):
        return None
    if not isinstance(architecture, dict) or not isinstance(pricing, dict):
        return None
    output_modalities = architecture.get("output_modalities")
    if not isinstance(output_modalities, list) or "text" not in output_modalities:
        return None

    input_price = _finite_number(pricing.get("prompt"))
    output_price = _finite_number(pricing.get("completion"))
    if input_price is None or output_price is None or input_price < 0 or output_price < 0:
        return None
    cache_price = _finite_number(pricing.get("input_cache_read"))
    if cache_price is not None and cache_price < 0:
        cache_price = None

    benchmark = raw.get("benchmarks")
    artificial_analysis = (
        benchmark.get("artificial_analysis") if isinstance(benchmark, dict) else None
    )
    intelligence = (
        _finite_number(artificial_analysis.get("intelligence_index"))
        if isinstance(artificial_analysis, dict)
        else None
    )
    context_length = _nonnegative_number(raw.get("context_length"))
    provider = name.partition(":")[0].strip() if ":" in name else model_id.partition("/")[0]
    input_per_million = input_price * 1_000_000
    output_per_million = output_price * 1_000_000
    return {
        "id": model_id,
        "canonical_slug": raw.get("canonical_slug"),
        "name": name,
        "provider": provider,
        "context_length": int(context_length) if context_length is not None else None,
        "modality": architecture.get("modality"),
        "input_price_per_token": input_price,
        "output_price_per_token": output_price,
        "input_price_per_million": round(input_per_million, 6),
        "output_price_per_million": round(output_per_million, 6),
        "cache_read_price_per_million": (
            round(cache_price * 1_000_000, 6) if cache_price is not None else None
        ),
        "blended_price_per_million": round((3 * input_per_million + output_per_million) / 4, 6),
        "is_free": input_price == 0 and output_price == 0,
        "is_open_weight": isinstance(raw.get("hugging_face_id"), str),
        "supports_reasoning": bool(raw.get("reasoning")),
        "intelligence_index": intelligence,
    }


def _ranking_model(
    row: dict[str, Any],
    models_by_key: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    for field in ("variant_permaslug", "model_permaslug"):
        key = row.get(field)
        if isinstance(key, str) and key in models_by_key:
            return models_by_key[key]
    return None


def _empty_index_bucket() -> dict[str, Any]:
    return {
        "total_tokens": 0.0,
        "priced_tokens": 0.0,
        "cost_usd": 0.0,
        "open_tokens": 0.0,
        "open_cost_usd": 0.0,
        "proprietary_tokens": 0.0,
        "proprietary_cost_usd": 0.0,
        "model_ids": set(),
    }


def _index_point(day: str, bucket: dict[str, Any]) -> dict[str, object] | None:
    priced_tokens = float(bucket["priced_tokens"])
    total_tokens = float(bucket["total_tokens"])
    if priced_tokens <= 0 or total_tokens <= 0:
        return None
    open_tokens = float(bucket["open_tokens"])
    proprietary_tokens = float(bucket["proprietary_tokens"])
    return {
        "date": day,
        "index_price": round(float(bucket["cost_usd"]) / priced_tokens * 1_000_000, 4),
        "open_price": (
            round(float(bucket["open_cost_usd"]) / open_tokens * 1_000_000, 4)
            if open_tokens > 0
            else None
        ),
        "proprietary_price": (
            round(float(bucket["proprietary_cost_usd"]) / proprietary_tokens * 1_000_000, 4)
            if proprietary_tokens > 0
            else None
        ),
        "total_tokens": int(total_tokens),
        "priced_tokens": int(priced_tokens),
        "coverage_pct": round(priced_tokens / total_tokens * 100, 2),
        "open_share_pct": round(open_tokens / priced_tokens * 100, 2),
        "model_count": len(bucket["model_ids"]),
    }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _nonnegative_number(value: object) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None
