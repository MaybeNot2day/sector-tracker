from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from app.services.ai_capex import AICapexService

PERIODS = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"]


def _history(symbol: str, capex: list[float]) -> dict[str, object]:
    return {
        "symbol": symbol,
        "history": [
            {
                "period_end": period,
                "capex": value,
                "revenue": value * 5,
            }
            for period, value in zip(PERIODS, capex, strict=True)
        ],
    }


@pytest.mark.asyncio
async def test_capex_service_composes_metrics_and_persists_history(tmp_path: Path) -> None:
    rows = {
        "MSFT": _history("MSFT", [10.0, 12.0, 14.0, 16.0, 20.0]),
        "AMZN": _history("AMZN", [5.0, 6.0, 7.0, 8.0, 10.0]),
    }

    def loader(symbol: str, name: str) -> dict[str, object] | None:
        return rows.get(symbol)

    database = tmp_path / "board.sqlite3"
    service = AICapexService(database, cache_seconds=0, loader=loader)
    payload = await service.get_capex()

    assert payload["as_of"] == "2026-03-31"
    assert payload["summary"] == {
        "companies": 2,
        "latest_reported_capex": 30.0,
        "ttm_capex": 93.0,
        "latest_period": "2026-03-31",
    }
    company_rows = cast(list[dict[str, Any]], payload["companies"])
    series = cast(list[dict[str, Any]], payload["series"])
    companies = {row["symbol"]: row for row in company_rows}
    assert companies["MSFT"]["qoq_pct"] == 25.0
    assert companies["MSFT"]["yoy_pct"] == 100.0
    assert companies["MSFT"]["capex_to_revenue_pct"] == 20.0
    assert series[-1] == {
        "period": "2026-Q1",
        "total_capex": 30.0,
        "company_count": 2,
    }

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_capex_history").fetchone() == (10,)


@pytest.mark.asyncio
async def test_capex_service_uses_persisted_history_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    seeded = AICapexService(
        database,
        cache_seconds=0,
        loader=lambda symbol, name: (
            _history("MSFT", [10.0, 12.0, 14.0, 16.0, 20.0]) if symbol == "MSFT" else None
        ),
    )
    await seeded.get_capex()

    restarted = AICapexService(database, cache_seconds=0, loader=lambda symbol, name: None)
    payload = await restarted.get_capex()

    summary = cast(dict[str, Any], payload["summary"])
    companies = cast(list[dict[str, Any]], payload["companies"])
    assert summary["companies"] == 1
    assert companies[0]["symbol"] == "MSFT"
