"""Official SOFR observations from the Federal Reserve Bank of New York."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app import db

SOFR_API_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/90.json"
SOFR_PAGE_URL = "https://www.newyorkfed.org/markets/reference-rates/sofr"
CACHE_SECONDS = 5 * 60.0
RELEASE_POLL_SECONDS = 60.0
REGULAR_POLL_SECONDS = 15 * 60.0
_NEW_YORK = ZoneInfo("America/New_York")


class SOFRError(RuntimeError):
    """The official SOFR dataset is unavailable and no persisted data exists."""


class SOFRService:
    def __init__(
        self,
        database_path: Path,
        *,
        client: httpx.AsyncClient | None = None,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self.database_path = database_path
        self.cache_seconds = cache_seconds
        self._client = client
        self._lock = asyncio.Lock()
        rows = db.load_sofr_history(database_path)
        self._payload = _payload_from_rows(rows, stale=True) if rows else None
        self._cache_at = 0.0

    async def get_payload(self) -> dict[str, object]:
        await self.refresh()
        if self._payload is None:
            raise SOFRError("SOFR data unavailable")
        return self._payload

    async def refresh(self, *, force: bool = False) -> bool:
        """Refresh official data; return whether the latest observation changed."""
        now = monotonic()
        if not force and self._payload is not None and now - self._cache_at < self.cache_seconds:
            return False
        async with self._lock:
            now = monotonic()
            if (
                not force
                and self._payload is not None
                and now - self._cache_at < self.cache_seconds
            ):
                return False
            before = _latest_key(self._payload)
            try:
                response = await self._http().get(SOFR_API_URL)
                response.raise_for_status()
                observations = _parse_observations(response.json())
                if not observations:
                    raise SOFRError("SOFR response carried no valid observations")
                fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                await asyncio.to_thread(
                    db.save_sofr_observations,
                    self.database_path,
                    observations,
                    fetched_at,
                )
                rows = await asyncio.to_thread(db.load_sofr_history, self.database_path)
                self._payload = _payload_from_rows(rows, stale=False)
            except (httpx.HTTPError, ValueError, TypeError, SOFRError) as exc:
                if self._payload is None:
                    rows = await asyncio.to_thread(db.load_sofr_history, self.database_path)
                    if rows:
                        self._payload = _payload_from_rows(rows, stale=True)
                if self._payload is None:
                    raise SOFRError("SOFR data unavailable") from exc
                self._payload = {
                    **self._payload,
                    "stale": True,
                    "warning": "New York Fed refresh failed; showing persisted data",
                }
            self._cache_at = now
            return before != _latest_key(self._payload)

    def snapshot(self) -> dict[str, object] | None:
        return self._payload

    def macro_item(self) -> dict[str, object] | None:
        payload = self._payload
        if payload is None:
            return None
        latest = payload.get("latest")
        if not isinstance(latest, dict):
            return None
        rate = latest.get("rate")
        change_bp = latest.get("change_bp")
        if not isinstance(rate, (int, float)):
            return None
        return {
            "symbol": "SOFR",
            "label": "SOFR",
            "unit": "yield",
            "last": float(rate),
            "change_abs": float(change_bp) / 100.0 if isinstance(change_bp, (int, float)) else None,
            "change_pct": None,
            "invert_tone": True,
            "is_stale": bool(payload.get("stale")),
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers={"Accept": "application/json", "User-Agent": "SectorTracker/0.1"},
            )
        return self._client


def sofr_poll_seconds(now: datetime | None = None) -> float:
    """Poll each minute across the NY Fed's approximately 08:00 ET release window."""
    current = (now or datetime.now(UTC)).astimezone(_NEW_YORK)
    minute = current.hour * 60 + current.minute
    if current.weekday() < 5 and 7 * 60 + 55 <= minute < 9 * 60 + 30:
        return RELEASE_POLL_SECONDS
    return REGULAR_POLL_SECONDS


def _parse_observations(payload: Any) -> list[dict[str, object]]:
    raw_rows = payload.get("refRates") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list):
        return []
    observations: list[dict[str, object]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or str(raw.get("type") or "").upper() != "SOFR":
            continue
        effective_date = str(raw.get("effectiveDate") or "")
        try:
            date.fromisoformat(effective_date)
        except ValueError:
            continue
        rate = _number(raw.get("percentRate", raw.get("percent")))
        if rate is None or rate < 0:
            continue
        observations.append(
            {
                "effective_date": effective_date,
                "rate": rate,
                "percentile_1": _number(raw.get("percentPercentile1")),
                "percentile_25": _number(raw.get("percentPercentile25")),
                "percentile_75": _number(raw.get("percentPercentile75")),
                "percentile_99": _number(raw.get("percentPercentile99")),
                "volume_billions": _number(raw.get("volumeInBillions")),
                "revision_indicator": str(raw.get("revisionIndicator") or ""),
            }
        )
    observations.sort(key=lambda row: str(row["effective_date"]), reverse=True)
    return observations


def _payload_from_rows(rows: list[dict[str, object]], *, stale: bool) -> dict[str, object]:
    latest = dict(rows[0])
    previous = rows[1] if len(rows) > 1 else None
    previous_rate = previous.get("rate") if isinstance(previous, dict) else None
    latest_rate = latest.get("rate")
    latest["change_bp"] = (
        round((float(latest_rate) - float(previous_rate)) * 100.0, 2)
        if isinstance(latest_rate, (int, float)) and isinstance(previous_rate, (int, float))
        else None
    )
    series = [
        {
            "date": str(row["effective_date"]),
            "rate": row["rate"],
            "volume_billions": row.get("volume_billions"),
        }
        for row in reversed(rows)
    ]
    return {
        "as_of": str(latest.get("fetched_at") or ""),
        "source": {
            "name": "Federal Reserve Bank of New York",
            "url": SOFR_PAGE_URL,
            "api_url": SOFR_API_URL,
        },
        "latest": latest,
        "series": series,
        "stale": stale,
    }


def _latest_key(payload: dict[str, object] | None) -> tuple[object, ...] | None:
    latest = payload.get("latest") if payload else None
    if not isinstance(latest, dict):
        return None
    return (
        latest.get("effective_date"),
        latest.get("rate"),
        latest.get("percentile_1"),
        latest.get("percentile_25"),
        latest.get("percentile_75"),
        latest.get("percentile_99"),
        latest.get("volume_billions"),
        latest.get("revision_indicator"),
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
