from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.models import AssetType, Bar, ProviderName, Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS latest_quotes (
    symbol TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    last REAL NOT NULL,
    previous_close REAL,
    change_abs REAL,
    change_pct REAL,
    timestamp TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    currency TEXT
);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (symbol, provider, interval, timestamp)
);
"""


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "latest_quotes", "currency", "TEXT")


def save_quotes(path: Path, quotes: Sequence[Quote]) -> None:
    if not quotes:
        return
    init_db(path)
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO latest_quotes (
                symbol, asset_type, provider, last, previous_close, change_abs, change_pct,
                timestamp, is_stale, error, currency
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                asset_type = excluded.asset_type,
                provider = excluded.provider,
                last = excluded.last,
                previous_close = excluded.previous_close,
                change_abs = excluded.change_abs,
                change_pct = excluded.change_pct,
                timestamp = excluded.timestamp,
                is_stale = excluded.is_stale,
                error = excluded.error,
                currency = excluded.currency
            """,
            [
                (
                    quote.symbol,
                    quote.asset_type,
                    quote.provider,
                    quote.last,
                    quote.previous_close,
                    quote.change_abs,
                    quote.change_pct,
                    _to_iso(quote.timestamp),
                    int(quote.is_stale),
                    quote.error,
                    quote.currency,
                )
                for quote in quotes
            ],
        )


def load_latest_quote(path: Path, symbol: str) -> Quote | None:
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT symbol, asset_type, provider, last, previous_close, change_abs, change_pct,
                   timestamp, is_stale, error, currency
            FROM latest_quotes
            WHERE symbol = ?
            """,
            (symbol.upper(),),
        ).fetchone()
    if row is None:
        return None
    return _quote_from_row(row)


def save_bars(path: Path, bars: Sequence[Bar]) -> None:
    if not bars:
        return
    init_db(path)
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO bars (
                symbol, provider, interval, timestamp, open, high, low, close, volume
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, provider, interval, timestamp) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume
            """,
            [
                (
                    bar.symbol,
                    bar.provider,
                    bar.interval,
                    _to_iso(bar.timestamp),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                )
                for bar in bars
            ],
        )


def load_bars(
    path: Path,
    symbol: str,
    interval: str,
    provider: ProviderName | None = None,
    *,
    limit: int | None = None,
) -> list[Bar]:
    init_db(path)
    params: list[object] = [symbol.upper(), interval]
    provider_clause = ""
    if provider:
        provider_clause = "AND provider = ?"
        params.append(provider)
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, provider, interval, timestamp, open, high, low, close, volume
            FROM bars
            WHERE symbol = ? AND interval = ?
            {provider_clause}
            ORDER BY timestamp DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
    return [_bar_from_row(row) for row in reversed(rows)]


def load_bars_by_symbol(
    path: Path,
    interval: str,
    *,
    limit_per_series: int = 260,
) -> dict[tuple[str, ProviderName], list[Bar]]:
    """Load cached history in one query, grouped by symbol and provider."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, provider, interval, timestamp, open, high, low, close, volume
            FROM bars
            WHERE interval = ?
            ORDER BY symbol, provider, timestamp
            """,
            (interval,),
        ).fetchall()

    grouped: dict[tuple[str, ProviderName], list[Bar]] = {}
    for row in rows:
        bar = _bar_from_row(row)
        grouped.setdefault((bar.symbol, bar.provider), []).append(bar)
    return {key: bars[-limit_per_series:] for key, bars in grouped.items()}


def mark_stale(quote: Quote, *, error: str | None = None) -> Quote:
    return replace(quote, is_stale=True, error=error or quote.error)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _quote_from_row(row: sqlite3.Row) -> Quote:
    return Quote(
        symbol=str(row["symbol"]),
        asset_type=cast(AssetType, row["asset_type"]),
        provider=cast(ProviderName, row["provider"]),
        last=float(row["last"]),
        previous_close=_optional_float(row["previous_close"]),
        change_abs=_optional_float(row["change_abs"]),
        change_pct=_optional_float(row["change_pct"]),
        timestamp=_from_iso(str(row["timestamp"])),
        is_stale=bool(row["is_stale"]),
        error=cast(str | None, row["error"]),
        currency=cast(str | None, row["currency"]),
    )


def _bar_from_row(row: sqlite3.Row) -> Bar:
    return Bar(
        symbol=str(row["symbol"]),
        provider=cast(ProviderName, row["provider"]),
        interval=str(row["interval"]),
        timestamp=_from_iso(str(row["timestamp"])),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=_optional_float(row["volume"]),
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
