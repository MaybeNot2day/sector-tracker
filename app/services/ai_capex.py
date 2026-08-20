"""Reported hyperscaler capex history used as an AI infrastructure proxy."""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast

import yfinance as yf

from app import db

CAPEX_CACHE_SECONDS = 6 * 60 * 60
AI_CAPEX_COMPANIES = (
    ("MSFT", "Microsoft"),
    ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet"),
    ("META", "Meta"),
    ("ORCL", "Oracle"),
    ("NVDA", "Nvidia"),
    ("AVGO", "Broadcom"),
    ("AMD", "AMD"),
)


class AICapexError(RuntimeError):
    """No current or persisted capex observations are available."""


class AICapexService:
    """Fetch quarterly statements and retain point-in-time history in SQLite."""

    def __init__(
        self,
        database_path: Path,
        *,
        cache_seconds: float = CAPEX_CACHE_SECONDS,
        loader: Callable[[str, str], dict[str, object] | None] | None = None,
    ) -> None:
        self.database_path = database_path
        self.cache_seconds = cache_seconds
        self._loader = loader or _load_company_history
        self._cache: tuple[float, dict[str, object]] | None = None
        self._lock = asyncio.Lock()

    async def get_capex(self) -> dict[str, object]:
        now = monotonic()
        if self._cache is not None and now - self._cache[0] < self.cache_seconds:
            return self._cache[1]

        async with self._lock:
            now = monotonic()
            if self._cache is not None and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]

            results = await asyncio.gather(
                *(
                    asyncio.to_thread(self._loader, symbol, name)
                    for symbol, name in AI_CAPEX_COMPANIES
                ),
                return_exceptions=True,
            )
            live_rows: list[dict[str, object]] = []
            for result in results:
                if isinstance(result, BaseException) or result is None:
                    continue
                history = result.get("history")
                symbol = result.get("symbol")
                if not isinstance(history, list) or not isinstance(symbol, str):
                    continue
                for row in history:
                    if isinstance(row, dict):
                        live_rows.append({"symbol": symbol, **row})

            if live_rows:
                await asyncio.to_thread(db.save_ai_capex_history, self.database_path, live_rows)
            stored = await asyncio.to_thread(db.load_ai_capex_history, self.database_path)
            if not stored:
                if self._cache is not None:
                    return self._cache[1]
                raise AICapexError("ai_capex_unavailable")

            payload = _compose_payload(stored)
            self._cache = (monotonic(), payload)
            return payload


def _load_company_history(symbol: str, name: str) -> dict[str, object] | None:
    ticker = yf.Ticker(symbol)
    cashflow = ticker.quarterly_cashflow
    income = ticker.quarterly_income_stmt
    capex = _statement_row(cashflow, ("Capital Expenditure", "Purchase Of PPE"))
    if capex is None:
        return None
    revenue = _statement_row(income, ("Total Revenue",))
    revenue_by_date = _series_by_date(revenue) if revenue is not None else {}

    history: list[dict[str, object]] = []
    for period_end, raw_value in _series_by_date(capex).items():
        value = _number(raw_value)
        if value is None:
            continue
        history.append(
            {
                "period_end": period_end,
                "capex": abs(value),
                "revenue": revenue_by_date.get(period_end),
            }
        )
    history.sort(key=lambda row: str(row["period_end"]))
    if not history:
        return None
    return {"symbol": symbol, "name": name, "history": history[-12:]}


def _statement_row(statement: Any, labels: tuple[str, ...]) -> Any | None:
    if statement is None or getattr(statement, "empty", True):
        return None
    for label in labels:
        if label in statement.index:
            row = statement.loc[label]
            if getattr(row, "ndim", 1) > 1:
                row = row.iloc[0]
            return row
    return None


def _series_by_date(series: Any) -> dict[str, float]:
    values: dict[str, float] = {}
    for timestamp, raw_value in series.items():
        value = _number(raw_value)
        if value is None:
            continue
        if hasattr(timestamp, "date"):
            period_end = timestamp.date().isoformat()
        else:
            period_end = str(timestamp)[:10]
        if len(period_end) == 10:
            values[period_end] = value
    return values


def _compose_payload(stored: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    names = dict(AI_CAPEX_COMPANIES)
    companies: list[dict[str, object]] = []
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total_capex": 0.0, "symbols": set()}
    )
    for symbol, name in AI_CAPEX_COMPANIES:
        history = sorted(stored.get(symbol, []), key=lambda row: str(row["period_end"]))[-12:]
        if not history:
            continue
        for row in history:
            quarter = _calendar_quarter(str(row["period_end"]))
            aggregate[quarter]["total_capex"] += float(cast(float, row["capex"]))
            aggregate[quarter]["symbols"].add(symbol)
        latest = history[-1]
        latest_capex = float(cast(float, latest["capex"]))
        previous_capex = float(cast(float, history[-2]["capex"])) if len(history) >= 2 else None
        year_ago_capex = float(cast(float, history[-5]["capex"])) if len(history) >= 5 else None
        revenue = _number(latest.get("revenue"))
        companies.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, name),
                "period_end": latest["period_end"],
                "latest_capex": latest_capex,
                "ttm_capex": sum(float(cast(float, row["capex"])) for row in history[-4:]),
                "qoq_pct": _pct_change(latest_capex, previous_capex),
                "yoy_pct": _pct_change(latest_capex, year_ago_capex),
                "capex_to_revenue_pct": (
                    round(latest_capex / revenue * 100, 2) if revenue and revenue > 0 else None
                ),
                "history": history,
            }
        )
    if not companies:
        raise AICapexError("ai_capex_empty")
    companies.sort(key=lambda row: float(cast(float, row["ttm_capex"])), reverse=True)

    series = [
        {
            "period": period,
            "total_capex": round(float(bucket["total_capex"]), 2),
            "company_count": len(bucket["symbols"]),
        }
        for period, bucket in sorted(aggregate.items())[-12:]
    ]
    as_of = max(str(company["period_end"]) for company in companies)
    return {
        "as_of": as_of,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "name": "Yahoo Finance company statements",
            "url": "https://finance.yahoo.com/",
        },
        "methodology": {
            "label": "Reported total capex",
            "description": (
                "Quarterly capital expenditure from company cash-flow statements. This is an "
                "AI infrastructure proxy: issuers do not consistently isolate AI-only spend."
            ),
        },
        "summary": {
            "companies": len(companies),
            "latest_reported_capex": round(
                sum(float(cast(float, company["latest_capex"])) for company in companies), 2
            ),
            "ttm_capex": round(
                sum(float(cast(float, company["ttm_capex"])) for company in companies), 2
            ),
            "latest_period": as_of,
        },
        "series": series,
        "companies": companies,
    }


def _calendar_quarter(period_end: str) -> str:
    parsed = datetime.fromisoformat(period_end)
    return f"{parsed.year}-Q{(parsed.month - 1) // 3 + 1}"


def _pct_change(current: float, previous: float | None) -> float | None:
    if previous is None or previous == 0:
        return None
    return round((current / previous - 1) * 100, 2)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
