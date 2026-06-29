from __future__ import annotations

import asyncio
import re
import subprocess
import time
from datetime import UTC, datetime

FARSIDE_READER_URL = "https://r.jina.ai/http://r.jina.ai/http://{url}"
FARSIDE_ASSETS = {
    "BTC": {
        "name": "BTC Spot ETFs",
        "url": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "parser": "token",
    },
    "ETH": {
        "name": "ETH Spot ETFs",
        "url": "https://farside.co.uk/ethereum-etf-flow-all-data/",
        "parser": "pipe",
    },
    "SOL": {
        "name": "SOL Spot ETFs",
        "url": "https://farside.co.uk/sol/",
        "parser": "pipe",
    },
}
MILLION = 1_000_000


class CryptoEtfFlowService:
    def __init__(self, *, cache_seconds: int = 900) -> None:
        self.cache_seconds = cache_seconds
        self._cache_payload: dict[str, object] | None = None
        self._cache_time = 0.0

    async def get_flows(self) -> dict[str, object]:
        if self._cache_payload and time.monotonic() - self._cache_time < self.cache_seconds:
            return self._cache_payload

        try:
            assets = await asyncio.to_thread(self._fetch_assets_sync)
        except Exception as exc:
            if self._cache_payload:
                cached = dict(self._cache_payload)
                cached["is_stale"] = True
                cached["error"] = "farside_fetch_failed"
                return cached
            return _unavailable("farside_fetch_failed", detail=str(exc))

        payload: dict[str, object] = {
            "status": "ok",
            "source": "farside",
            "updated_at": datetime.now(UTC).isoformat(),
            "is_stale": False,
            "assets": assets,
        }
        self._cache_payload = payload
        self._cache_time = time.monotonic()
        return payload

    def _fetch_assets_sync(self) -> list[dict[str, object]]:
        assets: list[dict[str, object]] = []
        for symbol, config in FARSIDE_ASSETS.items():
            markdown = _fetch_markdown(str(config["url"]))
            if config["parser"] == "pipe":
                rows = parse_pipe_table(markdown)
            else:
                rows = parse_token_table(markdown)
            assets.append(summarize_flow_asset(symbol, str(config["name"]), rows))
        return assets


def parse_token_table(markdown: str) -> list[dict[str, object]]:
    title_index = markdown.find("Bitcoin ETF Flow")
    table_text = markdown[title_index:] if title_index >= 0 else markdown
    tokens = [_clean_token(line) for line in table_text.splitlines()]
    tokens = [token for token in tokens if token]
    try:
        header_start = tokens.index("Date")
        total_index = tokens.index("Total", header_start)
    except ValueError:
        return []

    tickers = tokens[header_start + 1 : total_index]
    row_size = len(tickers) + 2
    rows: list[dict[str, object]] = []
    index = total_index + 1
    while index + row_size <= len(tokens):
        chunk = tokens[index : index + row_size]
        date = _parse_date(chunk[0])
        if date is None:
            break
        flow_values = [_parse_flow_millions(value) for value in chunk[1:-1]]
        total = _parse_flow_millions(chunk[-1])
        if total is not None:
            rows.append(_flow_row(date, tickers, flow_values, total))
        index += row_size
    return rows


def parse_pipe_table(markdown: str) -> list[dict[str, object]]:
    table_rows = [_pipe_cells(line) for line in markdown.splitlines() if line.startswith("|")]
    ticker_row = next((row for row in table_rows if _is_ticker_row(row)), None)
    if ticker_row is None:
        return []
    tickers = ticker_row[1:-1]
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
        "flow_usd": total * MILLION,
        "price_usd": None,
        "etf_flows": [
            {"ticker": ticker, "flow_usd": flow * MILLION}
            for ticker, flow in zip(tickers, flow_values, strict=False)
            if flow is not None
        ],
    }


def _clean_token(value: str) -> str:
    return value.strip().strip("|").strip()


def _pipe_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_ticker_row(row: list[str]) -> bool:
    if len(row) < 4 or row[0] != "":
        return False
    tickers = [cell for cell in row[1:-1] if cell]
    return bool(tickers) and all(re.fullmatch(r"[A-Z0-9]+", ticker) for ticker in tickers)


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%d %b %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_flow_millions(value: str) -> float | None:
    cleaned = value.strip().replace(",", "").replace("*", "")
    if not cleaned or cleaned == "-":
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
        return True
    flow = row.get("flow_usd")
    return isinstance(flow, int | float) and float(flow) != 0.0


def _sum_recent(rows: list[dict[str, object]], count: int) -> float | None:
    flows = [
        float(row["flow_usd"])
        for row in rows[-count:]
        if isinstance(row.get("flow_usd"), int | float)
    ]
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
