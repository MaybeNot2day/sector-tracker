"""Cloud GPU rental pricing from ComputePrices."""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import urljoin

import httpx

from app import db

GPU_PRICES_URL = "https://computeprices.com/api/v1/gpu-prices"
GPU_PUBLIC_URL = "https://computeprices.com/gpu"
REQUEST_TIMEOUT_SECONDS = 30.0
_PUBLIC_DETAIL_LIMIT = 8
_HEADERS = {
    "Accept": "application/json, text/html;q=0.9",
    "User-Agent": "SectorTracker/0.1 (+AI market dashboard)",
}
_PRICE_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")
_RANGE_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)\s*[–-]\s*\$([0-9]+(?:\.[0-9]+)?)")
_INTEGER_RE = re.compile(r"\d+")
_COMMITMENT_RE = re.compile(r"(\d+)mo", re.IGNORECASE)


class GPUComputeError(RuntimeError):
    """The upstream GPU pricing dataset is unavailable or malformed."""


class _TableParser(HTMLParser):
    """Collect visible table cells and links without another parser dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, object]]] = []
        self._row: list[dict[str, object]] | None = None
        self._cell: dict[str, object] | None = None
        self._link: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"parts": [], "links": []}
        elif tag == "a" and self._cell is not None:
            href = dict(attrs).get("href") or ""
            self._link = {"href": href, "parts": []}

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            cast(list[str], self._cell["parts"]).append(data)
        if self._link is not None:
            cast(list[str], self._link["parts"]).append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None and self._cell is not None:
            cast(list[dict[str, str]], self._cell["links"]).append(
                {
                    "href": str(self._link["href"]),
                    "text": _clean_text(cast(list[str], self._link["parts"])),
                }
            )
            self._link = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(
                {
                    "text": _clean_text(cast(list[str], self._cell["parts"])),
                    "links": self._cell["links"],
                }
            )
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None
            self._link = None


class GPUComputeService:
    """Fetch, normalize, cache, and persist cloud GPU rental prices."""

    def __init__(
        self,
        database_path: Path,
        *,
        api_key: str = "",
        cache_seconds: float = 6 * 60 * 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.database_path = database_path
        self.api_key = api_key.strip()
        self.cache_seconds = cache_seconds
        self._client = client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers=_HEADERS,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._cache: tuple[float, dict[str, object]] | None = None

    async def get_hardware(self) -> dict[str, object]:
        now = monotonic()
        if self._cache is not None and now - self._cache[0] < self.cache_seconds:
            return self._cache[1]
        async with self._lock:
            now = monotonic()
            if self._cache is not None and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            errors: list[str] = []
            if self.api_key:
                try:
                    payload = await self._fetch_api()
                    await asyncio.to_thread(
                        db.save_ai_gpu_compute_snapshot, self.database_path, payload
                    )
                    self._cache = (now, payload)
                    return payload
                except (GPUComputeError, httpx.HTTPError) as exc:
                    errors.append(f"API: {exc}")
            try:
                payload = await self._fetch_public_pages()
                if errors:
                    payload["warning"] = "; ".join(errors)
                await asyncio.to_thread(
                    db.save_ai_gpu_compute_snapshot, self.database_path, payload
                )
                self._cache = (now, payload)
                return payload
            except (GPUComputeError, httpx.HTTPError) as exc:
                errors.append(f"public page: {exc}")
            stale = await asyncio.to_thread(
                db.load_latest_ai_gpu_compute_snapshot, self.database_path
            )
            if stale is not None:
                stale["stale"] = True
                stale["warning"] = "; ".join(errors)
                self._cache = (now, stale)
                return stale
            raise GPUComputeError("ComputePrices unavailable: " + "; ".join(errors))

    async def _fetch_api(self) -> dict[str, object]:
        response = await self._client.get(
            GPU_PRICES_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
            raise GPUComputeError("API response has no data array")
        raw_rows = cast(list[object], raw["data"])
        rows = [cast(dict[str, Any], row) for row in raw_rows if isinstance(row, dict)]
        offers: list[dict[str, object]] = []
        for row in rows:
            offer = _normalize_api_offer(row)
            if offer is not None:
                offers.append(offer)
        if not offers:
            raise GPUComputeError("API returned no valid GPU prices")
        meta_raw = raw.get("meta")
        meta = cast(dict[str, Any], meta_raw) if isinstance(meta_raw, dict) else {}
        generated_at = str(meta.get("generated_at") or datetime.now(UTC).isoformat())
        return _compose_payload(
            offers,
            generated_at=generated_at,
            mode="authenticated_api",
            tier=str(meta.get("tier") or "unknown"),
        )

    async def _fetch_public_pages(self) -> dict[str, object]:
        response = await self._client.get(GPU_PUBLIC_URL)
        response.raise_for_status()
        models = _parse_public_catalog(response.text)
        if not models:
            raise GPUComputeError("public GPU table has no valid rows")
        detail_models = models[:_PUBLIC_DETAIL_LIMIT]
        responses = await asyncio.gather(
            *(
                self._client.get(urljoin(GPU_PUBLIC_URL, f"/gpus/{model['slug']}"))
                for model in detail_models
            ),
            return_exceptions=True,
        )
        offers_by_slug: dict[str, list[dict[str, object]]] = {}
        for model, detail_response in zip(detail_models, responses, strict=True):
            if isinstance(detail_response, BaseException) or not detail_response.is_success:
                continue
            offers = _parse_public_offers(
                detail_response.text, str(model["slug"]), str(model["name"])
            )
            if offers:
                offers_by_slug[str(model["slug"])] = offers
        all_offers = [offer for offers in offers_by_slug.values() for offer in offers]
        if not all_offers:
            raise GPUComputeError("public GPU detail pages have no valid provider prices")
        latest_date = max(
            (str(offer["updated_at"]) for offer in all_offers if offer.get("updated_at")),
            default=datetime.now(UTC).date().isoformat(),
        )
        for model in models:
            model["offers"] = offers_by_slug.get(str(model["slug"]), [])
        return _compose_public_payload(models, latest_date)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _links(cell: dict[str, object]) -> list[dict[str, str]]:
    return cast(list[dict[str, str]], cell.get("links", []))


def _parse_public_catalog(body: str) -> list[dict[str, object]]:
    parser = _TableParser()
    parser.feed(body)
    models: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in parser.rows:
        gpu_link = next(
            (link for cell in row for link in _links(cell) if link["href"].startswith("/gpus/")),
            None,
        )
        if gpu_link is None or len(row) < 5:
            continue
        slug = gpu_link["href"].removeprefix("/gpus/").strip("/")
        if not slug or slug in seen:
            continue
        texts = [str(cell.get("text", "")) for cell in row]
        price_cell = next((text for text in texts if _PRICE_RE.search(text) and "/hr" in text), "")
        range_cell = next((text for text in texts if _RANGE_RE.search(text)), "")
        if not price_cell or not range_cell:
            continue
        average_match = _PRICE_RE.search(price_cell)
        range_match = _RANGE_RE.search(range_cell)
        if average_match is None or range_match is None:
            continue
        vram_cell = next((text for text in texts if re.fullmatch(r"\d+\s*GB", text)), "")
        provider_cell = texts[-1]
        provider_count = int(provider_cell) if provider_cell.isdigit() else 0
        seen.add(slug)
        models.append(
            {
                "slug": slug,
                "name": gpu_link["text"],
                "vram_gb": _first_integer(vram_cell),
                "architecture": None,
                "average_price": float(average_match.group(1)),
                "min_price": float(range_match.group(1)),
                "max_price": float(range_match.group(2)),
                "provider_count": provider_count,
                "offers": [],
            }
        )
    return models


def _parse_public_offers(body: str, gpu_slug: str, gpu_name: str) -> list[dict[str, object]]:
    parser = _TableParser()
    parser.feed(body)
    offers: list[dict[str, object]] = []
    seen: set[tuple[str, float, str, str | None]] = set()
    for row in parser.rows:
        if len(row) < 4:
            continue
        provider_link = next(
            (link for link in _links(row[0]) if link["href"].startswith("/providers/")),
            None,
        )
        price_text = str(row[1].get("text", ""))
        price_match = _PRICE_RE.search(price_text)
        if provider_link is None or price_match is None:
            continue
        source_url = next(
            (
                link["href"]
                for link in reversed(_links(row[-1]))
                if link["href"].startswith(("https://", "http://"))
            ),
            None,
        )
        price = float(price_match.group(1))
        commitment_match = _COMMITMENT_RE.search(price_text)
        commitment_months = int(commitment_match.group(1)) if commitment_match else None
        updated_at = _parse_display_date(str(row[3].get("text", "")))
        identity = (provider_link["text"], price, str(row[2].get("text", "")), source_url)
        if identity in seen:
            continue
        seen.add(identity)
        offers.append(
            {
                "provider": provider_link["text"],
                "provider_slug": provider_link["href"].removeprefix("/providers/").strip("/"),
                "gpu": gpu_name,
                "gpu_slug": gpu_slug,
                "vram_gb": None,
                "architecture": None,
                "gpu_count": 1,
                "price_per_hour_usd": price,
                "total_hourly_usd": price,
                "pricing_type": "reserved" if commitment_months else "on_demand",
                "commitment_months": commitment_months,
                "config": str(row[2].get("text", "")),
                "source_url": source_url,
                "updated_at": updated_at,
            }
        )
    offers.sort(key=lambda offer: float(cast(float, offer["price_per_hour_usd"])))
    return offers


def _normalize_api_offer(raw: dict[str, Any]) -> dict[str, object] | None:
    price = _finite_number(raw.get("price_per_hour_usd"))
    provider = str(raw.get("provider") or "").strip()
    gpu = str(raw.get("gpu") or "").strip()
    gpu_slug = str(raw.get("gpu_slug") or "").strip()
    if price is None or price < 0 or not provider or not gpu or not gpu_slug:
        return None
    gpu_count = max(1, int(_finite_number(raw.get("gpu_count")) or 1))
    total = _finite_number(raw.get("total_hourly_usd"))
    source_url_raw = str(raw.get("source_url") or "")
    return {
        "provider": provider,
        "provider_slug": str(raw.get("provider_slug") or ""),
        "gpu": gpu,
        "gpu_slug": gpu_slug,
        "vram_gb": _optional_integer(raw.get("vram_gb")),
        "architecture": str(raw.get("architecture") or "") or None,
        "gpu_count": gpu_count,
        "price_per_hour_usd": price,
        "total_hourly_usd": total if total is not None else price * gpu_count,
        "pricing_type": str(raw.get("pricing_type") or "on_demand"),
        "commitment_months": _optional_integer(raw.get("commitment_months")),
        "config": f"{gpu_count}×",
        "source_url": source_url_raw
        if source_url_raw.startswith(("https://", "http://"))
        else None,
        "updated_at": str(raw.get("last_updated") or ""),
    }


def _compose_payload(
    offers: list[dict[str, object]],
    *,
    generated_at: str,
    mode: str,
    tier: str,
) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for offer in offers:
        grouped.setdefault(str(offer["gpu_slug"]), []).append(offer)
    models: list[dict[str, object]] = []
    for slug, model_offers in grouped.items():
        model_offers.sort(key=lambda item: float(cast(float, item["price_per_hour_usd"])))
        prices = [float(cast(float, offer["price_per_hour_usd"])) for offer in model_offers]
        first = model_offers[0]
        providers = {str(offer["provider_slug"]) for offer in model_offers}
        models.append(
            {
                "slug": slug,
                "name": str(first["gpu"]),
                "vram_gb": first.get("vram_gb"),
                "architecture": first.get("architecture"),
                "average_price": round(sum(prices) / len(prices), 4),
                "min_price": min(prices),
                "max_price": max(prices),
                "provider_count": len(providers),
                "offers": model_offers,
            }
        )
    models.sort(key=lambda model: (-int(cast(int, model["provider_count"])), str(model["name"])))
    return {
        "as_of": generated_at,
        "generated_at": generated_at,
        "source": {
            "name": "ComputePrices API",
            "url": "https://computeprices.com/docs/api",
            "mode": mode,
            "tier": tier,
            "attribution": "Cloud GPU prices normalized to USD per GPU-hour by ComputePrices.",
        },
        "summary": _summary(models),
        "models": models,
    }


def _compose_public_payload(models: list[dict[str, object]], latest_date: str) -> dict[str, object]:
    return {
        "as_of": latest_date,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "name": "ComputePrices public GPU pages",
            "url": GPU_PUBLIC_URL,
            "mode": "public_page",
            "tier": "public",
            "attribution": (
                "Public cloud GPU comparison pages; detailed offers loaded "
                "for the most-covered models."
            ),
        },
        "summary": _summary(models),
        "models": models,
    }


def _summary(models: list[dict[str, object]]) -> dict[str, object]:
    offers = [
        offer
        for model in models
        for offer in cast(list[dict[str, object]], model.get("offers", []))
    ]
    providers = {str(offer.get("provider_slug") or offer.get("provider")) for offer in offers}
    prices = [
        float(cast(float, offer["price_per_hour_usd"]))
        for offer in offers
        if _finite_number(offer.get("price_per_hour_usd")) is not None
    ]
    return {
        "models": len(models),
        "detailed_models": sum(bool(model.get("offers")) for model in models),
        "offers": len(offers),
        "providers": len(providers),
        "lowest_price": min(prices) if prices else None,
    }


def _parse_display_date(value: str) -> str | None:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value.strip() or None


def _first_integer(value: str) -> int | None:
    match = _INTEGER_RE.search(value)
    return int(match.group()) if match else None


def _optional_integer(value: object) -> int | None:
    number = _finite_number(value)
    return int(number) if number is not None else None


def _finite_number(value: object) -> float | None:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
