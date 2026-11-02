"""Free AI market data composed from OpenRouter's public catalog and rankings."""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any, NamedTuple, cast

import httpx

from app import db

MODELS_URL = "https://openrouter.ai/api/v1/models"
RANKINGS_URL = "https://openrouter.ai/api/frontend/v1/rankings/models?view=week"
REQUEST_TIMEOUT_SECONDS = 20.0
# A failed OpenRouter fetch waits this long before the next attempt; until
# then requests get the last good rows (flagged stale) without a round trip.
FAILURE_RETRY_SECONDS = 120.0
# The week rankings view carries every model only for its newest day; older
# days keep a handful of leftover rows. A day priced from fewer models is a
# sample, not an index point (live complete days carry ~300+ models).
MIN_INDEX_MODELS = 100
# Frontier tier: proprietary models at the capability frontier. The main rule
# is data-driven — an Artificial Analysis intelligence index of 45 or above —
# because the leading labs' flagship model families (GPT, Claude Opus,
# Gemini, Grok) score at least that high while cheap minis/lites stay out.
# The explicit roots cover freshly shipped flagship variants (and "Pro"/"Fast"
# editions of the current release) before their AA index row lands.
FRONTIER_INTELLIGENCE_MIN = 45.0
FRONTIER_EXTRA_PREFIXES = (
    "openai/gpt-5.6-sol-pro",
    "openai/gpt-5.6-terra-pro",
    "openai/gpt-5.6-luna-pro",
    "openai/gpt-5.5-pro",
    "anthropic/claude-opus-latest",
    "anthropic/claude-sonnet-latest",
    "x-ai/grok-4.20",
)
CHINA_MODEL_PROVIDER_PREFIXES = frozenset(
    {
        "baidu",
        "bytedance",
        "bytedance-seed",
        "deepseek",
        "dots-studio",
        "inclusionai",
        "kwaipilot",
        "meituan",
        "minimax",
        "moonshotai",
        "qwen",
        "stepfun",
        "tencent",
        "xiaomi",
        "z-ai",
    }
)
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "SectorTracker/0.1 (+AI market dashboard)",
}


class AIDataError(RuntimeError):
    """The upstream AI dataset is unavailable or malformed."""


class _Upstream(NamedTuple):
    rows: list[dict[str, Any]]
    fetched_at: datetime
    stale: bool


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
        # kind -> (monotonic load time, rows, wall-clock fetch time)
        self._caches: dict[str, tuple[float, list[dict[str, Any]], datetime]] = {}
        self._failed_at: dict[str, float] = {}
        self._locks = {"models": asyncio.Lock(), "rankings": asyncio.Lock()}

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def get_models(self) -> dict[str, object]:
        upstream = await self._get_models_raw()
        models = [model for raw in upstream.rows if (model := _normalize_model(raw)) is not None]
        models.sort(key=lambda item: (str(item["provider"]), str(item["name"])))

        observed_date = upstream.fetched_at.date().isoformat()
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
            "as_of": upstream.fetched_at.isoformat(),
            "stale": upstream.stale,
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
        models_upstream, rankings_upstream = await asyncio.gather(
            self._get_models_raw(),
            self._get_rankings_raw(),
        )
        catalog = _catalog_by_slug_variant(models_upstream.rows)

        aggregates: dict[str, dict[str, Any]] = defaultdict(_empty_index_bucket)
        detail_rows: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for raw in rankings_upstream.rows:
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
            match = _ranking_model(raw, catalog)
            if match is None:
                continue
            priced, model = match
            input_price = _finite_number(priced.get("input_price_per_token"))
            output_price = _finite_number(priced.get("output_price_per_token"))
            if input_price is None or output_price is None:
                continue

            cost = prompt_tokens * input_price + completion_tokens * output_price
            model_id = str(model["id"])
            bucket["priced_tokens"] += tokens
            bucket["cost_usd"] += cost
            bucket["model_ids"].add(model_id)
            segment = "open" if bool(model["is_open_weight"]) else "proprietary"
            bucket[f"{segment}_tokens"] += tokens
            bucket[f"{segment}_cost_usd"] += cost
            is_frontier = _is_frontier(model)
            if is_frontier:
                bucket["frontier_tokens"] += tokens
                bucket["frontier_cost_usd"] += cost
            is_china = _is_china_model(model)
            if is_china:
                bucket["china_tokens"] += tokens
                bucket["china_cost_usd"] += cost
            detail = detail_rows[day].setdefault(
                model_id,
                {
                    "id": model_id,
                    "name": model["name"],
                    "provider": model["provider"],
                    "is_open_weight": model["is_open_weight"],
                    "is_frontier": is_frontier,
                    "is_china": is_china,
                    "tokens": 0.0,
                    "cost_usd": 0.0,
                },
            )
            detail["tokens"] += tokens
            detail["cost_usd"] += cost

        # The week view only carries the newest day in full; older days hold a
        # handful of leftover rows whose "index" would be a 1-6 model sample.
        live_series = [
            point
            for day in sorted(aggregates)
            if (point := _index_point(day, aggregates[day])) is not None
            and int(cast(int, point["model_count"])) >= MIN_INDEX_MODELS
        ]
        if not live_series:
            raise AIDataError("token_index_empty")

        await asyncio.to_thread(db.save_ai_token_index_points, self.database_path, live_series)
        series = await asyncio.to_thread(
            db.load_ai_token_index_points,
            self.database_path,
            365,
        )
        series_by_date = {str(point["date"]): point for point in series}

        latest_live = live_series[-1]
        latest_date = str(latest_live["date"])
        latest_details = list(detail_rows.get(latest_date, {}).values())
        priced_tokens = _finite_number(latest_live["priced_tokens"]) or 0.0
        total_cost = sum(float(item["cost_usd"]) for item in latest_details)
        for item in latest_details:
            tokens = float(item["tokens"])
            cost = float(item.pop("cost_usd"))
            item["tokens"] = int(tokens)
            item["effective_price"] = round(cost / tokens * 1_000_000, 4)
            item["usage_share_pct"] = round(tokens / priced_tokens * 100, 2)
            item["cost_share_pct"] = round(cost / total_cost * 100, 2) if total_cost > 0 else 0.0
        latest_details.sort(key=lambda item: int(item["tokens"]), reverse=True)

        return {
            "as_of": latest_date,
            "generated_at": datetime.now(UTC).isoformat(),
            "stale": models_upstream.stale or rankings_upstream.stale,
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
                    "proxy based on a published Hugging Face ID. Proprietary is the "
                    "complement: every priced model without a published HF ID, not only "
                    "Anthropic and OpenAI. Frontier is the capability-leading subset of "
                    "proprietary models — proprietary with an Artificial Analysis "
                    "intelligence index of 45 or more, plus the newest flagship "
                    "variants whose index row has not landed yet. The China index includes "
                    "only models from an explicit list of mainland-China model creators."
                ),
                "formula": (
                    "sum(prompt_tokens * input_price + completion_tokens * output_price) "
                    "/ sum(prompt_tokens + completion_tokens) * 1,000,000"
                ),
                "constituent_rule": (
                    "Every ranked model with an exact current OpenRouter catalog slug and "
                    "text pricing joins automatically on refresh. Batch and free variants "
                    f"are priced at their own catalog rate. Days with fewer than "
                    f"{MIN_INDEX_MODELS} priced models are not recorded."
                ),
                "open_weight_proxy": True,
                "china_provider_prefixes": sorted(CHINA_MODEL_PROVIDER_PREFIXES),
            },
            # The stored point wins when it already covered more of the day.
            "latest": series_by_date.get(latest_date, latest_live),
            "series": series,
            "top_models": latest_details[:15],
        }

    async def _get_models_raw(self) -> _Upstream:
        return await self._get_cached(MODELS_URL, "models")

    async def _get_rankings_raw(self) -> _Upstream:
        return await self._get_cached(RANKINGS_URL, "rankings")

    async def _get_cached(self, url: str, kind: str) -> _Upstream:
        cached = self._cached_or_backoff(kind)
        if cached is not None:
            return cached

        async with self._locks[kind]:
            cached = self._cached_or_backoff(kind)
            if cached is not None:
                return cached
            try:
                response = await self._http_client().get(url, headers=_HEADERS)
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise AIDataError(f"{kind}_payload_invalid")
                typed_rows = list(rows)
            except (httpx.HTTPError, ValueError, AIDataError) as exc:
                self._failed_at[kind] = monotonic()
                previous = self._caches.get(kind)
                if previous is None:
                    raise AIDataError(f"{kind}_unavailable") from exc
                return _Upstream(previous[1], previous[2], stale=True)

            self._failed_at.pop(kind, None)
            fetched_at = datetime.now(UTC)
            self._caches[kind] = (monotonic(), typed_rows, fetched_at)
            return _Upstream(typed_rows, fetched_at, stale=False)

    def _cached_or_backoff(self, kind: str) -> _Upstream | None:
        """Fresh cache, or the stale cache while a failed refresh cools down.

        Returns None when an upstream fetch is due. During the cooldown
        without any cache this fails fast instead of hitting OpenRouter again.
        """
        now = monotonic()
        cached = self._caches.get(kind)
        if cached is not None and now - cached[0] < self.cache_seconds:
            return _Upstream(cached[1], cached[2], stale=False)
        failed_at = self._failed_at.get(kind)
        if failed_at is None or now - failed_at >= FAILURE_RETRY_SECONDS:
            return None
        if cached is None:
            raise AIDataError(f"{kind}_unavailable")
        return _Upstream(cached[1], cached[2], stale=True)

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
    hugging_face_id = raw.get("hugging_face_id")
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
        # OpenRouter sends "" (not null) for many proprietary models.
        "is_open_weight": isinstance(hugging_face_id, str) and bool(hugging_face_id.strip()),
        "supports_reasoning": bool(raw.get("reasoning")),
        "intelligence_index": intelligence,
    }


def _catalog_by_slug_variant(
    raw_models: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, object]]:
    """Index normalized catalog models by (permaslug, variant suffix).

    Rankings name a model by its dated canonical slug, which OpenRouter shares
    between the standard entry and its ":batch"/":free" siblings; the variant
    suffix picks the entry whose pricing applies ("" for standard).
    """
    catalog: dict[tuple[str, str], dict[str, object]] = {}
    for raw in raw_models:
        model = _normalize_model(raw)
        if model is None:
            continue
        base_id, _, variant = str(model["id"]).partition(":")
        for slug in (raw.get("canonical_slug"), base_id):
            if isinstance(slug, str) and slug:
                catalog.setdefault((slug, variant), model)
    return catalog


def _ranking_model(
    row: dict[str, Any],
    catalog: dict[tuple[str, str], dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Return (catalog entry whose prices apply, model the usage counts toward).

    Variant traffic is priced at the variant's own rate but attributed to the
    standard model, so one model never appears twice in the leaderboard.
    """
    slug = row.get("model_permaslug")
    if not isinstance(slug, str) or not slug:
        return None
    variant_slug = row.get("variant_permaslug")
    variant = variant_slug.partition(":")[2] if isinstance(variant_slug, str) else ""
    priced = catalog.get((slug, variant))
    if priced is None:
        return None
    return priced, catalog.get((slug, ""), priced)


def _empty_index_bucket() -> dict[str, Any]:
    return {
        "total_tokens": 0.0,
        "priced_tokens": 0.0,
        "cost_usd": 0.0,
        "open_tokens": 0.0,
        "open_cost_usd": 0.0,
        "proprietary_tokens": 0.0,
        "proprietary_cost_usd": 0.0,
        "frontier_tokens": 0.0,
        "frontier_cost_usd": 0.0,
        "china_tokens": 0.0,
        "china_cost_usd": 0.0,
        "model_ids": set(),
    }


def _index_point(day: str, bucket: dict[str, Any]) -> dict[str, object] | None:
    priced_tokens = float(bucket["priced_tokens"])
    total_tokens = float(bucket["total_tokens"])
    if priced_tokens <= 0 or total_tokens <= 0:
        return None
    open_tokens = float(bucket["open_tokens"])
    proprietary_tokens = float(bucket["proprietary_tokens"])
    frontier_tokens = float(bucket["frontier_tokens"])
    china_tokens = float(bucket["china_tokens"])
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
        "frontier_price": (
            round(float(bucket["frontier_cost_usd"]) / frontier_tokens * 1_000_000, 4)
            if frontier_tokens > 0
            else None
        ),
        "china_price": (
            round(float(bucket["china_cost_usd"]) / china_tokens * 1_000_000, 4)
            if china_tokens > 0
            else None
        ),
        "total_tokens": int(total_tokens),
        "priced_tokens": int(priced_tokens),
        "coverage_pct": round(priced_tokens / total_tokens * 100, 2),
        "open_share_pct": round(open_tokens / priced_tokens * 100, 2),
        "model_count": len(bucket["model_ids"]),
    }


def _is_frontier(model: dict[str, Any]) -> bool:
    """Whether a normalized model belongs to the proprietary capability frontier."""
    if bool(model["is_open_weight"]):
        return False
    intelligence = _finite_number(model.get("intelligence_index"))
    if intelligence is not None:
        return intelligence >= FRONTIER_INTELLIGENCE_MIN
    slug = str(model.get("canonical_slug") or model.get("id") or "")
    return slug.startswith(FRONTIER_EXTRA_PREFIXES)


def _is_china_model(model: dict[str, Any]) -> bool:
    """Whether the model creator is based in mainland China."""
    slug = str(model.get("id") or model.get("canonical_slug") or "")
    provider_prefix = slug.partition("/")[0].removeprefix("~")
    return provider_prefix in CHINA_MODEL_PROVIDER_PREFIXES


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
