import asyncio
import threading
import time
from typing import Any, cast

import pytest

import app.services.crypto_etf_flows as crypto_etf_flows
from app.services.crypto_etf_flows import (
    CryptoEtfFlowService,
    parse_farside_table,
    parse_pipe_table,
    parse_token_table,
    summarize_flow_asset,
)


def test_parse_token_table_normalizes_bitcoin_farside_rows() -> None:
    markdown = """
Bitcoin ETF Flow - All Data (US$m)
Date
IBIT
FBTC
GBTC
Total

11 Jan 2024
111.7
227.0
(95.1)
243.6

12 Jan 2024
386.0
195.3
(484.1)
97.2
"""

    rows = parse_token_table(markdown)
    payload = summarize_flow_asset("BTC", "BTC Spot ETFs", rows)

    assert rows[0]["date"] == "2024-01-11"
    assert rows[0]["flow_usd"] == 243_600_000
    assert rows[0]["etf_flows"][2]["flow_usd"] == -95_100_000  # type: ignore[index]
    assert payload["latest_date"] == "2024-01-12"
    assert payload["latest_flow_usd"] == 97_200_000
    assert payload["five_day_flow_usd"] == 340_800_000


def test_parse_pipe_table_normalizes_ethereum_farside_rows() -> None:
    markdown = """
|  | Blackrock | Fidelity | Grayscale | Total |
| --- | --- | --- | --- | --- |
|  | ETHA | FETH | ETHE |  |
| Fee | 0.25% | 0.25% | 2.50% |  |
| Seed | 10.6 | 4.4 | 9,199.3* | 10,360 |
| 23 Jul 2024 | 266.5 | 71.3 | (484.1) | (146.3) |
| 24 Jul 2024 | 17.4 | - | (326.9) | (309.5) |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("ETH", "ETH Spot ETFs", rows)

    assert rows[0]["date"] == "2024-07-23"
    assert rows[0]["flow_usd"] == -146_300_000
    assert rows[0]["etf_flows"][0]["ticker"] == "ETHA"  # type: ignore[index]
    assert rows[0]["etf_flows"][2]["flow_usd"] == -484_100_000  # type: ignore[index]
    assert payload["latest_date"] == "2024-07-24"
    assert payload["laggards"][0]["ticker"] == "ETHE"  # type: ignore[index]


def test_parse_pipe_table_handles_date_header_farside_rows() -> None:
    markdown = """
| Date | IBIT | FBTC | GBTC | BTC | Total |
| --- | --- | --- | --- | --- | --- |
| 26 Jun 2026 | (444.5) | - | 0.0 | - | (444.5) |
| 29 Jun 2026 | - | - | - | - | 0.0 |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("BTC", "BTC Spot ETFs", rows)

    assert rows[0]["date"] == "2026-06-26"
    assert rows[0]["flow_usd"] == -444_500_000
    assert rows[0]["etf_flows"][0]["ticker"] == "IBIT"  # type: ignore[index]
    assert payload["latest_date"] == "2026-06-26"
    assert payload["latest_flow_usd"] == -444_500_000
    assert payload["laggards"][0]["ticker"] == "IBIT"  # type: ignore[index]


def test_parse_token_table_handles_plain_text_farside_rows() -> None:
    markdown = """
Ethereum ETF Flow – All Data (US$m)

Blackrock
Fidelity
Grayscale
Total

ETHA
FETH
ETHE
ETH

Fee
0.25%
0.25%
2.50%
0.15%

Seed
10.6
4.4
9,199.3*
1,022.5*
10,360

23 Jul 2024
266.5
71.3
(484.1)
15.1
(131.2)

24 Jul 2024
17.4
74.5
(326.9)
-
(235.0)
"""

    rows = parse_token_table(markdown)
    payload = summarize_flow_asset("ETH", "ETH Spot ETFs", rows)

    assert rows[0]["date"] == "2024-07-23"
    assert rows[0]["flow_usd"] == -131_200_000
    assert rows[0]["etf_flows"][0]["ticker"] == "ETHA"  # type: ignore[index]
    assert rows[0]["etf_flows"][2]["flow_usd"] == -484_100_000  # type: ignore[index]
    assert payload["latest_date"] == "2024-07-24"
    assert payload["latest_flow_usd"] == -235_000_000
    assert payload["laggards"][0]["ticker"] == "ETHE"  # type: ignore[index]


def test_parse_farside_table_falls_back_between_table_shapes() -> None:
    pipe_markdown = """
| Date | IBIT | FBTC | Total |
| --- | --- | --- | --- |
| 26 Jun 2026 | (444.5) | - | (444.5) |
"""
    text_markdown = """
Ethereum ETF Flow – All Data (US$m)
Blackrock
Total
ETHA
Fee
0.25%
Seed
10.6
10.6
23 Jul 2024
266.5
266.5
"""

    assert parse_farside_table(pipe_markdown)[0]["date"] == "2026-06-26"
    assert parse_farside_table(text_markdown)[0]["date"] == "2024-07-23"


def test_parse_pipe_table_handles_solana_farside_rows() -> None:
    markdown = """
|  | Bitwise | VanEck | Grayscale | Total |
| --- | --- | --- | --- | --- |
|  | BSOL | VSOL | GSOL |  |
| 25 Jun 2026 | (3.9) | 0.0 | 0.0 | (3.9) |
| 26 Jun 2026 | 2.0 | - | 0.0 | 2.0 |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("SOL", "SOL Spot ETFs", rows)

    assert rows[-1]["date"] == "2026-06-26"
    assert rows[-1]["flow_usd"] == 2_000_000
    assert payload["leaders"][0]["ticker"] == "BSOL"  # type: ignore[index]


def test_summarize_ignores_blank_current_day_placeholder() -> None:
    markdown = """
|  | Bitwise | VanEck | Grayscale | Total |
| --- | --- | --- | --- | --- |
|  | BSOL | VSOL | GSOL |  |
| 25 Jun 2026 | (3.9) | 0.0 | 0.0 | (3.9) |
| 26 Jun 2026 | 2.0 | - | 0.0 | 2.0 |
| 29 Jun 2026 | - | - | - | 0.0 |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("SOL", "SOL Spot ETFs", rows)

    assert rows[-1]["date"] == "2026-06-29"
    assert rows[-1]["etf_flows"] == []
    assert payload["latest_date"] == "2026-06-26"
    assert payload["latest_flow_usd"] == 2_000_000
    assert payload["five_day_flow_usd"] == -1_900_000
    assert payload["leaders"][0]["ticker"] == "BSOL"  # type: ignore[index]
    assert payload["rows"][-1]["date"] == "2026-06-26"  # type: ignore[index]


def test_summarize_ignores_all_zero_current_day_placeholder() -> None:
    markdown = """
| Date | IBIT | FBTC | GBTC | Total |
| --- | --- | --- | --- | --- |
| 26 Jun 2026 | (444.5) | - | 0.0 | (444.5) |
| 29 Jun 2026 | 0.0 | 0.0 | 0.0 | 0.0 |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("BTC", "BTC Spot ETFs", rows)

    assert rows[-1]["date"] == "2026-06-29"
    assert rows[-1]["etf_flows"] == [
        {"ticker": "IBIT", "flow_usd": 0},
        {"ticker": "FBTC", "flow_usd": 0},
        {"ticker": "GBTC", "flow_usd": 0},
    ]
    assert payload["latest_date"] == "2026-06-26"
    assert payload["latest_flow_usd"] == -444_500_000
    assert payload["laggards"][0]["ticker"] == "IBIT"  # type: ignore[index]


def test_fetch_assets_sync_is_bounded_concurrent_ordered_and_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CryptoEtfFlowService()
    lock = threading.Lock()
    barrier = threading.Barrier(3)
    active = 0
    max_active = 0
    calls: list[str] = []
    completed: list[str] = []
    delays = {"BTC": 0.15, "ETH": 0.10, "SOL": 0.05}
    failure: str | None = None

    def fake_fetch_markdown(url: str) -> str:
        nonlocal active, max_active
        symbol = next(
            symbol
            for symbol, config in crypto_etf_flows.FARSIDE_ASSETS.items()
            if config["url"] == url
        )
        with lock:
            calls.append(symbol)
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=1)
            time.sleep(delays[symbol])
            if symbol == failure:
                raise RuntimeError(f"{symbol} failed")
            with lock:
                completed.append(symbol)
            return """
| Date | FUND | Total |
| --- | --- | --- |
| 1 Jul 2026 | 1.0 | 1.0 |
"""
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(crypto_etf_flows, "_fetch_markdown", fake_fetch_markdown)

    started = time.monotonic()
    assets = service._fetch_assets_sync()
    elapsed = time.monotonic() - started

    assert max_active == 3
    assert sorted(calls) == ["BTC", "ETH", "SOL"]
    assert completed == ["SOL", "ETH", "BTC"]
    assert [asset["asset"] for asset in assets] == ["BTC", "ETH", "SOL"]
    assert elapsed < 0.30  # bounded by the slowest 0.15s request, not their 0.30s sum

    calls.clear()
    completed.clear()
    barrier = threading.Barrier(3)
    failure = "ETH"
    surviving_assets = service._fetch_assets_sync()

    assert sorted(calls) == ["BTC", "ETH", "SOL"]
    assert [asset["asset"] for asset in surviving_assets] == ["BTC", "SOL"]


def test_cold_partial_fetch_reports_missing_assets_as_stale() -> None:
    service = CryptoEtfFlowService()
    service._fetch_assets_sync = lambda: [  # type: ignore[method-assign]
        {"asset": "BTC", "name": "BTC Spot ETFs"},
    ]

    payload = asyncio.run(service.get_flows())

    assert payload["is_stale"] is True
    assert payload["error"] == "farside_partial_data"
    assert payload["missing_assets"] == ["ETH", "SOL"]
    assets = cast(list[dict[str, Any]], payload["assets"])
    assert [entry["asset"] for entry in assets] == ["BTC"]


def test_get_flows_carries_cached_assets_through_partial_fetch() -> None:
    # Regression: a partial cycle (only BTC survives) used to replace the
    # full cached payload with status ok / is_stale False, hiding the good
    # ETH/SOL data for the whole cache TTL.
    service = CryptoEtfFlowService()
    service._fetch_assets_sync = lambda: [  # type: ignore[method-assign]
        {"asset": "BTC", "name": "BTC Spot ETFs"},
        {"asset": "ETH", "name": "ETH Spot ETFs"},
        {"asset": "SOL", "name": "SOL Spot ETFs"},
    ]
    first = asyncio.run(service.get_flows())
    assert first["is_stale"] is False

    service._cache_time = -1e9  # expire the TTL so the next call refetches
    service._fetch_assets_sync = lambda: [  # type: ignore[method-assign]
        {"asset": "BTC", "name": "BTC Spot ETFs"},
    ]
    second = asyncio.run(service.get_flows())

    assert second["is_stale"] is True
    assets = cast(list[dict[str, Any]], second["assets"])
    assert [entry["asset"] for entry in assets] == ["BTC", "ETH", "SOL"]
