from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from app import db

logger = logging.getLogger(__name__)

FARSIDE_READER_URL = "https://r.jina.ai/http://{url}"
FARSIDE_ASSETS = {
    "BTC": {
        "name": "BTC Spot ETFs",
        "url": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    },
    "ETH": {
        "name": "ETH Spot ETFs",
        "url": "https://farside.co.uk/ethereum-etf-flow-all-data/",
    },
    "SOL": {
        "name": "SOL Spot ETFs",
        "url": "https://farside.co.uk/sol/",
    },
}
MILLION = 1_000_000


class CryptoEtfFlowService:
    # A failed Farside cycle waits this long before the next attempt; every
    # request re-running three 30s curls during an outage (with no herd
    # guard) meant up to ~90s latency per caller, multiplied by clients.
    FAILURE_RETRY_SECONDS = 120

    def __init__(self, *, cache_seconds: int = 900, database_path: Path | None = None) -> None:
        self.cache_seconds = cache_seconds
        # When set, every successful fetch accrues its per-day rows into
        # etf_flow_history so /api/market-context can serve flows beyond
        # Farside's ~20-day scrape window.
        self.database_path = database_path
        self._cache_payload: dict[str, object] | None = None
        self._cache_time = 0.0
        # None (not 0.0): monotonic() is near-zero right after host boot,
        # which would otherwise read as a live failure cooldown.
        self._failed_time: float | None = None
        self._lock = asyncio.Lock()

    async def get_flows(self) -> dict[str, object]:
        if self._cache_is_fresh():
            return self._cache_payload or {}
        if (
            self._failed_time is not None
            and time.monotonic() - self._failed_time < self.FAILURE_RETRY_SECONDS
        ):
            return self._degraded("farside_fetch_failed")
        async with self._lock:
            # Herd guard: concurrent callers wait here; whoever lost the
            # race returns the winner's fresh cache instead of refetching.
            if self._cache_is_fresh():
                return self._cache_payload or {}
            if (
                self._failed_time is not None
                and time.monotonic() - self._failed_time < self.FAILURE_RETRY_SECONDS
            ):
                return self._degraded("farside_fetch_failed")
            try:
                assets = await asyncio.to_thread(self._fetch_assets_sync)
            except Exception as exc:
                self._failed_time = time.monotonic()
                return self._degraded("farside_fetch_failed", detail=str(exc))
            if not assets:
                self._failed_time = time.monotonic()
                return self._degraded("farside_no_data")
            # A partial cycle (BTC ok, ETH/SOL curls die) must not overwrite
            # cached survivors with an "ok" payload missing them: carry the
            # previous entries forward for pages that failed this round and
            # mark the payload stale. Only a fully fresh fetch is is_stale
            # False.
            fetched = {str(entry["asset"]): entry for entry in assets}
            if self.database_path is not None:
                try:
                    await asyncio.to_thread(
                        db.upsert_etf_flow_history, self.database_path, _history_rows(assets)
                    )
                except Exception:
                    # History is a bonus; the flows payload must survive it.
                    logger.warning("etf flow history persist failed", exc_info=True)
            cached_assets = self._cache_payload.get("assets") if self._cache_payload else None
            cached_by_symbol = (
                {str(entry.get("asset")): entry for entry in cached_assets}
                if isinstance(cached_assets, list)
                else {}
            )
            missing = [symbol for symbol in FARSIDE_ASSETS if symbol not in fetched]
            merged: list[dict[str, object]] = []
            for symbol in FARSIDE_ASSETS:
                entry = fetched.get(symbol)
                if entry is None:
                    entry = cached_by_symbol.get(symbol)
                    if entry is None:
                        continue
                merged.append(entry)
            payload: dict[str, object] = {
                "status": "ok",
                "source": "farside",
                "updated_at": datetime.now(UTC).isoformat(),
                "is_stale": bool(missing),
                "assets": merged,
            }
            if missing:
                payload["error"] = "farside_partial_data"
                payload["missing_assets"] = missing
            self._cache_payload = payload
            self._cache_time = time.monotonic()
            return payload

    def _cache_is_fresh(self) -> bool:
        if self._cache_payload is None:
            return False
        ttl = (
            self.FAILURE_RETRY_SECONDS
            if self._cache_payload.get("is_stale")
            else self.cache_seconds
        )
        return time.monotonic() - self._cache_time < ttl


    def _degraded(self, error: str, detail: str | None = None) -> dict[str, object]:
        if self._cache_payload:
            cached = dict(self._cache_payload)
            cached["is_stale"] = True
            cached["error"] = error
            return cached
        return _unavailable(error, detail=detail)

    def _fetch_assets_sync(self) -> list[dict[str, object]]:
        """Fetch every Farside page concurrently, keeping partial results."""
        configured_assets = [
            (index, symbol, str(config["name"]), str(config["url"]))
            for index, (symbol, config) in enumerate(FARSIDE_ASSETS.items())
        ]
        if not configured_assets:
            return []

        def fetch_one(
            index: int, symbol: str, name: str, url: str
        ) -> tuple[int, dict[str, object] | None]:
            try:
                markdown = _fetch_markdown(url)
                rows = parse_farside_table(markdown)
                payload = summarize_flow_asset(symbol, name, rows) if rows else None
            except Exception:
                payload = None
            return index, payload

        with ThreadPoolExecutor(max_workers=len(configured_assets)) as executor:
            futures = [
                executor.submit(fetch_one, index, symbol, name, url)
                for index, symbol, name, url in configured_assets
            ]
            completed = [future.result() for future in futures]

        return [
            payload
            for _, payload in sorted(completed)
            if payload is not None
        ]


def _history_rows(assets: list[dict[str, object]]) -> list[tuple[str, str, float]]:
    """Flatten freshly fetched asset summaries into (asset, date, flow_usd) rows."""
    rows: list[tuple[str, str, float]] = []
    for entry in assets:
        asset = str(entry.get("asset") or "")
        day_rows = entry.get("rows")
        if not asset or not isinstance(day_rows, list):
            continue
        for day in day_rows:
            if not isinstance(day, dict):
                continue
            flow = day.get("flow_usd")
            date = day.get("date")
            if isinstance(date, str) and isinstance(flow, int | float):
                rows.append((asset, date, float(flow)))
    return rows


def parse_farside_table(markdown: str) -> list[dict[str, object]]:
    for parser in (parse_pipe_table, parse_token_table):
        rows = parser(markdown)
        if rows:
            return rows
    return []


def parse_token_table(markdown: str) -> list[dict[str, object]]:
    tokens = [_clean_token(line) for line in markdown.splitlines()]
    tokens = [token for token in tokens if token]
    return _parse_date_header_token_table(tokens) or _parse_fee_seed_token_table(tokens)


def _parse_date_header_token_table(tokens: list[str]) -> list[dict[str, object]]:
    try:
        header_start = tokens.index("Date")
        total_index = tokens.index("Total", header_start)
    except ValueError:
        return []

    tickers = tokens[header_start + 1 : total_index]
    if not _is_usable_ticker_list(tickers):
        return []
    return _parse_token_date_rows(tokens, total_index + 1, tickers)


def _parse_fee_seed_token_table(tokens: list[str]) -> list[dict[str, object]]:
    for fee_index, token in enumerate(tokens):
        if token != "Fee":
            continue
        tickers = _ticker_block_before(tokens, fee_index)
        if not _is_usable_ticker_list(tickers):
            continue
        try:
            seed_index = tokens.index("Seed", fee_index + 1)
        except ValueError:
            continue
        row_start = seed_index + len(tickers) + 2
        rows = _parse_token_date_rows(tokens, row_start, tickers)
        if rows:
            return rows
    return []


def parse_pipe_table(markdown: str) -> list[dict[str, object]]:
    table_rows = [_pipe_cells(line) for line in markdown.splitlines() if line.startswith("|")]
    header_rows = _parse_pipe_date_header_rows(table_rows)
    if header_rows:
        return header_rows

    ticker_row = next((row for row in table_rows if _is_ticker_row(row)), None)
    if ticker_row is None:
        return []
    tickers = ticker_row[1:-1]
    if not _is_usable_ticker_list(tickers):
        return []
    rows: list[dict[str, object]] = []
    for row in table_rows:
        if len(row) < len(tickers) + 2:
            continue
        date = _parse_date(row[0])
        if date is None:
            continue
        flow_values = [_parse_flow_millions(value) for value in row[1 : 1 + len(tickers)]]
        total = _parse_flow_millions(row[1 + len(tickers)])
        if total is not None:
            rows.append(_flow_row(date, tickers, flow_values, total))
    return rows


def _parse_pipe_date_header_rows(table_rows: list[list[str]]) -> list[dict[str, object]]:
    header_row = next(
        (row for row in table_rows if row and row[0].casefold() == "date" and "Total" in row),
        None,
    )
    if header_row is None:
        return []

    total_index = header_row.index("Total")
    tickers = header_row[1:total_index]
    if not _is_usable_ticker_list(tickers):
        return []

    rows: list[dict[str, object]] = []
    for row in table_rows:
        if len(row) <= total_index:
            continue
        date = _parse_date(row[0])
        if date is None:
            continue
        flow_values = [_parse_flow_millions(value) for value in row[1:total_index]]
        total = _parse_flow_millions(row[total_index])
        if total is not None:
            rows.append(_flow_row(date, tickers, flow_values, total))
    return rows


def summarize_flow_asset(
    asset: str,
    name: str,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    rows = sorted(rows, key=lambda item: str(item["date"]))
    populated_rows = [row for row in rows if _is_populated_flow_row(row)]
    summary_rows = populated_rows or rows
    latest = summary_rows[-1] if summary_rows else None
    latest_etf_flows = latest["etf_flows"] if latest else []

    return {
        "asset": asset,
        "name": name,
        "latest_date": latest["date"] if latest else None,
        "latest_flow_usd": latest["flow_usd"] if latest else None,
        "latest_price_usd": None,
        "five_day_flow_usd": _sum_recent(summary_rows, 5),
        "ten_day_flow_usd": _sum_recent(summary_rows, 10),
        "leaders": _rank_etf_flows(latest_etf_flows, reverse=True),
        "laggards": _rank_etf_flows(latest_etf_flows, reverse=False),
        "rows": summary_rows[-20:],
    }


def _fetch_markdown(url: str) -> str:
    reader_url = FARSIDE_READER_URL.format(url=url)
    completed = subprocess.run(
        [
            "curl",
            "-fsSL",
            "-A",
            "Mozilla/5.0",
            "--max-time",
            "30",
            reader_url,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def _flow_row(
    date: datetime,
    tickers: list[str],
    flow_values: list[float | None],
    total: float,
) -> dict[str, object]:
    return {
        "date": date.date().isoformat(),
        "flow_usd": _millions_to_usd(total),
        "price_usd": None,
        "etf_flows": [
            {"ticker": ticker, "flow_usd": _millions_to_usd(flow)}
            for ticker, flow in zip(tickers, flow_values, strict=False)
            if flow is not None
        ],
    }


def _millions_to_usd(value: float) -> int:
    return round(value * MILLION)


def _clean_token(value: str) -> str:
    return value.strip().strip("|").strip()


def _pipe_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_ticker_row(row: list[str]) -> bool:
    if len(row) < 4 or row[0] != "":
        return False
    tickers = [cell for cell in row[1:-1] if cell]
    return _is_usable_ticker_list(tickers)


def _is_usable_ticker_list(tickers: list[str]) -> bool:
    return bool(tickers) and all(_is_ticker_symbol(ticker) for ticker in tickers)


def _is_ticker_symbol(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{2,8}", value))


def _ticker_block_before(tokens: list[str], end_index: int) -> list[str]:
    tickers: list[str] = []
    index = end_index - 1
    while index >= 0 and _is_ticker_symbol(tokens[index]):
        tickers.append(tokens[index])
        index -= 1
    return list(reversed(tickers))


def _parse_token_date_rows(
    tokens: list[str],
    start_index: int,
    tickers: list[str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    index = start_index
    row_size = len(tickers) + 2
    while index + row_size <= len(tokens):
        date = _parse_date(tokens[index])
        if date is None:
            index += 1
            continue
        flow_start = index + 1
        total_index = flow_start + len(tickers)
        flow_values = [_parse_flow_millions(value) for value in tokens[flow_start:total_index]]
        total = _parse_flow_millions(tokens[total_index])
        if total is not None:
            rows.append(_flow_row(date, tickers, flow_values, total))
        index = total_index + 1
    return rows


# strptime's %b matches LC_TIME month names, so under a non-English locale
# every Farside "11 Jan 2024" date failed to parse and the service reported
# no data. Map the English abbreviations explicitly instead.
_ENGLISH_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_date(value: str) -> datetime | None:
    parts = value.strip().split()
    if len(parts) != 3:
        return None
    day_text, month_text, year_text = parts
    month = _ENGLISH_MONTHS.get(month_text.lower())
    if month is None or not day_text.isdigit() or not year_text.isdigit():
        return None
    try:
        return datetime(int(year_text), month, int(day_text), tzinfo=UTC)
    except ValueError:
        return None


def _parse_flow_millions(value: str) -> float | None:
    cleaned = value.strip().replace(",", "").replace("*", "")
    if not cleaned or cleaned in {"-", "–", "—"}:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return -parsed if negative else parsed


def _rank_etf_flows(
    flows: object,
    *,
    reverse: bool,
    limit: int = 4,
) -> list[dict[str, object]]:
    if not isinstance(flows, list):
        return []
    usable = [
        flow
        for flow in flows
        if isinstance(flow, dict) and isinstance(flow.get("flow_usd"), int | float)
    ]
    filtered = [
        flow
        for flow in usable
        if (float(flow["flow_usd"]) > 0 if reverse else float(flow["flow_usd"]) < 0)
    ]
    return sorted(filtered, key=lambda item: float(item["flow_usd"]), reverse=reverse)[:limit]


def _is_populated_flow_row(row: dict[str, object]) -> bool:
    etf_flows = row.get("etf_flows")
    if isinstance(etf_flows, list) and etf_flows:
        for flow in etf_flows:
            if not isinstance(flow, dict):
                continue
            value = flow.get("flow_usd")
            if isinstance(value, int | float) and float(value) != 0.0:
                return True
    flow = row.get("flow_usd")
    return isinstance(flow, int | float) and float(flow) != 0.0


def _sum_recent(rows: list[dict[str, object]], count: int) -> float | None:
    flows: list[float] = []
    for row in rows[-count:]:
        value = row.get("flow_usd")
        if isinstance(value, int | float):
            flows.append(float(value))
    return sum(flows) if flows else None


def _unavailable(error: str, *, detail: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "unavailable",
        "source": "farside",
        "updated_at": datetime.now(UTC).isoformat(),
        "is_stale": False,
        "assets": [],
        "error": error,
    }
    if detail:
        payload["detail"] = detail
    return payload
