from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Any, TypeVar
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

# Listed equity options stop trading at the 16:00 ET close of their
# expiration day; "today" for expiry selection is the US Eastern date.
_EASTERN = ZoneInfo("America/New_York")
_EXPIRY_CLOSE_HOUR = 16
# Chain columns the snapshot actually reads. MarketData bills an options
# chain per contract when the response carries bid/ask/mid/last, but as a
# single credit without them — the free tier has 100 credits a day.
_CHAIN_COLUMNS = "side,strike,openInterest,gamma,iv,underlyingPrice,updated"

_K = TypeVar("_K")


def _evict_oldest(cache: dict[_K, tuple[float, Any]], max_entries: int) -> None:
    """Drop the oldest-stamped entries so long-running hosts stay bounded."""
    while len(cache) > max_entries:
        del cache[min(cache, key=lambda key: cache[key][0])]


class OptionsDataError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class MarketDataOptionsService:
    """Cached MarketData.app option-chain snapshots and dealer-gamma proxies."""

    EXPIRATIONS_CACHE_SECONDS = 900
    REQUEST_TIMEOUT_SECONDS = 12.0
    FAILURE_COOLDOWN_SECONDS = 30.0
    # A token locked to another device fails every request until the owner
    # resets the authorized device in the MarketData dashboard.
    DEVICE_LOCK_COOLDOWN_SECONDS = 30 * 60.0
    SNAPSHOT_CACHE_MAX = 64
    EXPIRATIONS_CACHE_MAX = 32
    SYMBOL_LOCKS_MAX = 64

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.marketdata.app",
        cache_seconds: int = 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.cache_seconds = cache_seconds
        self._client = client
        self._snapshot_cache: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
        self._expirations_cache: dict[str, tuple[float, list[str]]] = {}
        # Per-symbol locks: one symbol's slow chain fetch must not serialize
        # every other symbol behind it. Cooldown stamps remember the failing
        # error code so cooled requests reproduce the client-visible contract.
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._failure_until: dict[tuple[str, str], tuple[float, str]] = {}
        self._expiration_failure_until: dict[str, tuple[float, OptionsDataError]] = {}
        # Token-wide lockout (device lock): fail fast without any HTTP.
        self._token_blocked_until: tuple[float, str] | None = None

    async def get_expirations(self, symbol: str) -> list[str]:
        """Listed expiration dates (cached), without fetching any chain."""
        if not self.token:
            raise OptionsDataError("options_not_configured", status_code=503)
        clean_symbol = symbol.strip().upper()
        async with self._symbol_lock(clean_symbol):
            return list(await self._get_expirations(clean_symbol))

    async def get_snapshot(self, symbol: str, expiration: str | None = None) -> dict[str, object]:
        if not self.token:
            raise OptionsDataError("options_not_configured", status_code=503)

        clean_symbol = symbol.strip().upper()
        async with self._symbol_lock(clean_symbol):
            expirations = await self._get_expirations(clean_symbol)
            selected = _select_expiration(expirations, expiration)
            cache_key = (clean_symbol, selected)
            cached = self._snapshot_cache.get(cache_key)
            if cached is not None and monotonic() - cached[0] < self.cache_seconds:
                payload = dict(cached[1])
                payload["expirations"] = expirations
                return payload

            cooldown = self._failure_until.get(cache_key)
            if cooldown is not None and monotonic() < cooldown[0]:
                # Recently failed: serve the stale snapshot if one exists,
                # otherwise fail fast — no network until the cooldown lapses.
                if cached is None:
                    raise OptionsDataError(cooldown[1])
                payload = dict(cached[1])
                payload["is_stale"] = True
                payload["error"] = cooldown[1]
                payload["expirations"] = expirations
                return payload

            try:
                encoded_symbol = quote(clean_symbol, safe=".-")
                chain_payload = await self._get_json(
                    f"/v1/options/chain/{encoded_symbol}/",
                    {"expiration": selected, "columns": _CHAIN_COLUMNS},
                )
                spot, contracts, updated_at = _marketdata_chain(chain_payload)
                payload = build_options_snapshot(
                    clean_symbol,
                    selected,
                    expirations,
                    spot,
                    contracts,
                    source="marketdata",
                    updated_at=updated_at,
                )
            except OptionsDataError as exc:
                self._failure_until[cache_key] = (
                    monotonic() + self.FAILURE_COOLDOWN_SECONDS,
                    exc.code,
                )
                if cached is None:
                    raise
                payload = dict(cached[1])
                payload["is_stale"] = True
                payload["error"] = exc.code
                payload["expirations"] = expirations
                return payload

            self._failure_until.pop(cache_key, None)
            self._snapshot_cache[cache_key] = (monotonic(), payload)
            _evict_oldest(self._snapshot_cache, self.SNAPSHOT_CACHE_MAX)
            return dict(payload)

    def _symbol_lock(self, symbol: str) -> asyncio.Lock:
        lock = self._symbol_locks.get(symbol)
        if lock is None:
            if len(self._symbol_locks) >= self.SYMBOL_LOCKS_MAX:
                # Unbounded symbols would grow this dict forever; locks that
                # nobody holds are safe to drop (a waiter keeps its reference).
                for key, existing in list(self._symbol_locks.items()):
                    if not existing.locked():
                        del self._symbol_locks[key]
            lock = asyncio.Lock()
            self._symbol_locks[symbol] = lock
        return lock

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_expirations(self, symbol: str) -> list[str]:
        cached = self._expirations_cache.get(symbol)
        if cached is not None and monotonic() - cached[0] < self.EXPIRATIONS_CACHE_SECONDS:
            return cached[1]
        cooldown = self._expiration_failure_until.get(symbol)
        if cooldown is not None and monotonic() < cooldown[0]:
            if cached is not None:
                return cached[1]
            error = cooldown[1]
            raise OptionsDataError(error.code, status_code=error.status_code)
        try:
            encoded_symbol = quote(symbol, safe=".-")
            payload = await self._get_json(f"/v1/options/expirations/{encoded_symbol}/", {})
            expirations = _expiration_dates(payload)
            if not expirations:
                raise OptionsDataError("options_expirations_unavailable", status_code=404)
        except OptionsDataError as exc:
            self._expiration_failure_until[symbol] = (
                monotonic() + self.FAILURE_COOLDOWN_SECONDS,
                exc,
            )
            _evict_oldest(self._expiration_failure_until, self.EXPIRATIONS_CACHE_MAX)
            if cached is not None:
                return cached[1]
            raise
        self._expiration_failure_until.pop(symbol, None)
        self._expirations_cache[symbol] = (monotonic(), expirations)
        _evict_oldest(self._expirations_cache, self.EXPIRATIONS_CACHE_MAX)
        return expirations

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        blocked = self._token_blocked_until
        if blocked is not None and monotonic() < blocked[0]:
            raise OptionsDataError(blocked[1])
        try:
            response = await self._http_client().get(
                f"{self.base_url}{path}",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token}",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            failed = exc.response
            status = failed.status_code
            if status == 403 and _is_device_lock(failed.text):
                self._lock_token(failed.text)
                code = "marketdata_device_locked"
            elif status in {401, 403}:
                code = "marketdata_auth_failed"
            elif status == 402:
                code = "marketdata_entitlement_required"
            elif status == 429:
                logger.warning(
                    "MarketData rate limit hit on %s (remaining=%s, reset=%s)",
                    path,
                    failed.headers.get("x-api-ratelimit-remaining"),
                    failed.headers.get("x-api-ratelimit-reset"),
                )
                code = "marketdata_rate_limited"
            else:
                code = "marketdata_request_failed"
            raise OptionsDataError(code) from exc
        except httpx.HTTPError as exc:
            raise OptionsDataError("marketdata_unavailable") from exc

        if response.status_code == 204:
            raise OptionsDataError("marketdata_no_data", status_code=404)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OptionsDataError("marketdata_invalid_payload") from exc
        if not isinstance(payload, dict):
            raise OptionsDataError("marketdata_invalid_payload")
        if str(payload.get("s") or "").lower() == "error":
            message = str(payload.get("errmsg") or "")
            if _is_device_lock(message):
                self._lock_token(message)
                raise OptionsDataError("marketdata_device_locked")
            raise OptionsDataError("marketdata_request_failed")
        return payload

    def _lock_token(self, message: str) -> None:
        self._token_blocked_until = (
            monotonic() + self.DEVICE_LOCK_COOLDOWN_SECONDS,
            "marketdata_device_locked",
        )
        logger.warning(
            "MarketData token is locked to another device; pausing options requests "
            "for %.0f minutes: %s",
            self.DEVICE_LOCK_COOLDOWN_SECONDS / 60,
            message[:300],
        )

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT_SECONDS)
        return self._client


def build_options_snapshot(
    symbol: str,
    expiration: str,
    expirations: list[str],
    spot: float,
    contracts: list[dict[str, Any]],
    *,
    source: str,
    updated_at: str | None = None,
) -> dict[str, object]:
    by_strike: dict[float, dict[str, float | int]] = {}
    iv_by_strike: dict[float, list[float]] = {}
    contracts_with_greeks = 0
    valid_contracts = 0

    for contract in contracts:
        strike = _positive_float(contract.get("strike"))
        option_type = str(contract.get("option_type") or "").lower()
        if strike is None or option_type not in {"call", "put"}:
            continue
        valid_contracts += 1
        open_interest = _nonnegative_int(contract.get("open_interest"))
        contract_size = _positive_float(contract.get("contract_size")) or 100.0
        greeks = contract.get("greeks")
        greeks_map = greeks if isinstance(greeks, dict) else {}
        gamma = _nonnegative_float(greeks_map.get("gamma"))
        if gamma is not None:
            contracts_with_greeks += 1
        raw_gex = (gamma or 0.0) * open_interest * contract_size * spot * spot * 0.01

        row = by_strike.setdefault(
            strike,
            {
                "call_oi": 0,
                "put_oi": 0,
                "call_gex": 0.0,
                "put_gex": 0.0,
            },
        )
        if option_type == "call":
            row["call_oi"] = int(row["call_oi"]) + open_interest
            row["call_gex"] = float(row["call_gex"]) + raw_gex
        else:
            row["put_oi"] = int(row["put_oi"]) + open_interest
            row["put_gex"] = float(row["put_gex"]) - raw_gex

        iv = _positive_float(greeks_map.get("mid_iv"))
        if iv is None:
            iv = _positive_float(greeks_map.get("smv_vol"))
        if iv is not None and iv <= 10:
            iv_by_strike.setdefault(strike, []).append(iv)

    if not by_strike:
        raise OptionsDataError("options_chain_empty", status_code=404)

    strikes: list[dict[str, float | int]] = []
    for strike in sorted(by_strike):
        row = by_strike[strike]
        call_gex = float(row["call_gex"])
        put_gex = float(row["put_gex"])
        strikes.append(
            {
                "strike": strike,
                "call_oi": int(row["call_oi"]),
                "put_oi": int(row["put_oi"]),
                "call_gex": round(call_gex, 2),
                "put_gex": round(put_gex, 2),
                "net_gex": round(call_gex + put_gex, 2),
            }
        )

    total_call_oi = sum(int(row["call_oi"]) for row in strikes)
    total_put_oi = sum(int(row["put_oi"]) for row in strikes)
    call_wall = (
        max(strikes, key=lambda row: int(row["call_oi"]))["strike"] if total_call_oi else None
    )
    put_wall = max(strikes, key=lambda row: int(row["put_oi"]))["strike"] if total_put_oi else None
    nearest_iv_strike = min(iv_by_strike, key=lambda strike: abs(strike - spot), default=None)
    atm_ivs = iv_by_strike.get(nearest_iv_strike, []) if nearest_iv_strike is not None else []
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
    net_gex = sum(float(row["net_gex"]) for row in strikes)

    return {
        "status": "ok",
        "source": source,
        "methodology": "dealer_gamma_proxy",
        "symbol": symbol,
        "spot": spot,
        "expiration": expiration,
        "expirations": expirations,
        "updated_at": updated_at or datetime.now(UTC).isoformat(),
        "is_stale": False,
        "metrics": {
            "atm_iv": round(atm_iv, 6) if atm_iv is not None else None,
            "put_call_oi": round(total_put_oi / total_call_oi, 4) if total_call_oi else None,
            "net_gex": round(net_gex, 2),
            "call_wall": call_wall,
            "put_wall": put_wall,
            "max_pain": _max_pain(strikes) if total_call_oi or total_put_oi else None,
            "call_oi": total_call_oi,
            "put_oi": total_put_oi,
        },
        "strikes": strikes,
        "quality": {
            "contracts": valid_contracts,
            "greeks_coverage_pct": round(contracts_with_greeks / valid_contracts * 100, 1)
            if valid_contracts
            else 0.0,
        },
    }


def _select_expiration(expirations: list[str], requested: str | None) -> str:
    if requested is not None:
        try:
            date.fromisoformat(requested)
        except ValueError as exc:
            raise OptionsDataError("options_expiration_invalid", status_code=422) from exc
        if requested not in expirations:
            raise OptionsDataError("options_expiration_not_found", status_code=404)
        return requested

    now = datetime.now(_EASTERN)
    # After the 16:00 ET close today's expiration has stopped trading.
    first_open = now.date() + timedelta(days=1 if now.hour >= _EXPIRY_CLOSE_HOUR else 0)
    earliest = first_open.isoformat()
    return next((value for value in expirations if value >= earliest), expirations[0])


def _is_device_lock(message: str) -> bool:
    """MarketData's 403 for a token in use from another device/IP."""
    text = message.lower()
    return "only one device" in text or "blockedip" in text


def _expiration_dates(payload: dict[str, Any]) -> list[str]:
    raw_dates = payload.get("expirations")
    candidates = (
        [value for value in raw_dates if isinstance(value, str)]
        if isinstance(raw_dates, list)
        else []
    )
    valid: set[str] = set()
    for value in candidates:
        try:
            date.fromisoformat(value)
        except ValueError:
            continue
        valid.add(value)
    return sorted(valid)


def _marketdata_chain(
    payload: dict[str, Any],
) -> tuple[float, list[dict[str, Any]], str | None]:
    spot = _first_positive_float(payload.get("underlyingPrice"))
    if spot is None:
        raise OptionsDataError("marketdata_quote_unavailable")

    sides = payload.get("side")
    strikes = payload.get("strike")
    if not isinstance(sides, list) or not isinstance(strikes, list):
        raise OptionsDataError("options_chain_empty", status_code=404)

    contracts: list[dict[str, Any]] = []
    for index in range(min(len(sides), len(strikes))):
        contracts.append(
            {
                "strike": strikes[index],
                "option_type": sides[index],
                "open_interest": _column_value(payload.get("openInterest"), index),
                "contract_size": 100,
                "greeks": {
                    "gamma": _column_value(payload.get("gamma"), index),
                    "mid_iv": _column_value(payload.get("iv"), index),
                },
            }
        )
    if not contracts:
        raise OptionsDataError("options_chain_empty", status_code=404)
    return spot, contracts, _latest_timestamp(payload.get("updated"))


def _column_value(column: object, index: int) -> object:
    if not isinstance(column, list) or index >= len(column):
        return None
    return column[index]


def _first_positive_float(column: object) -> float | None:
    values = column if isinstance(column, list) else [column]
    for value in values:
        parsed = _positive_float(value)
        if parsed is not None:
            return parsed
    return None


def _latest_timestamp(column: object) -> str | None:
    values = column if isinstance(column, list) else [column]
    timestamps = [_positive_float(value) for value in values]
    latest = max((value for value in timestamps if value is not None), default=None)
    if latest is None:
        return None
    try:
        return datetime.fromtimestamp(latest, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _max_pain(strikes: list[dict[str, float | int]]) -> float:
    def payout(settlement: float) -> float:
        total = 0.0
        for row in strikes:
            strike = float(row["strike"])
            total += max(settlement - strike, 0.0) * int(row["call_oi"])
            total += max(strike - settlement, 0.0) * int(row["put_oi"])
        return total

    return float(min((float(row["strike"]) for row in strikes), key=payout))


def _positive_float(value: object) -> float | None:
    parsed = _float_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_float(value: object) -> float | None:
    parsed = _float_or_none(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _nonnegative_int(value: object) -> int:
    parsed = _float_or_none(value)
    return max(0, int(parsed)) if parsed is not None else 0


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed
