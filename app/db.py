from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import SupportsFloat, SupportsIndex, cast

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
    currency TEXT,
    display_last REAL,
    display_previous_close REAL,
    display_change_abs REAL,
    display_change_pct REAL,
    display_currency TEXT,
    volume REAL,
    funding_rate REAL,
    open_interest_usd REAL
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

CREATE INDEX IF NOT EXISTS idx_bars_interval_symbol_provider_timestamp
ON bars (interval, symbol, provider, timestamp DESC);

CREATE TABLE IF NOT EXISTS board_snapshots (
    snapshot_date TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    report_date TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (slug, report_date)
);
"""
_initialized_paths: set[Path] = set()
_init_lock = Lock()



def init_db(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved in _initialized_paths and resolved.exists():
        return

    with _init_lock:
        if resolved in _initialized_paths and resolved.exists():
            return
        _initialized_paths.discard(resolved)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with _connect(resolved) as conn:
            conn.executescript(SCHEMA)
            _ensure_column(conn, "latest_quotes", "currency", "TEXT")
            _ensure_column(conn, "latest_quotes", "display_last", "REAL")
            _ensure_column(conn, "latest_quotes", "display_previous_close", "REAL")
            _ensure_column(conn, "latest_quotes", "display_change_abs", "REAL")
            _ensure_column(conn, "latest_quotes", "display_change_pct", "REAL")
            _ensure_column(conn, "latest_quotes", "display_currency", "TEXT")
            _ensure_column(conn, "latest_quotes", "volume", "REAL")
            _ensure_column(conn, "latest_quotes", "funding_rate", "REAL")
            _ensure_column(conn, "latest_quotes", "open_interest_usd", "REAL")
        _initialized_paths.add(resolved)


def save_quotes(path: Path, quotes: Sequence[Quote]) -> None:
    if not quotes:
        return
    init_db(path)
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO latest_quotes (
                symbol, asset_type, provider, last, previous_close, change_abs, change_pct,
                timestamp, is_stale, error, currency, display_last, display_previous_close,
                display_change_abs, display_change_pct, display_currency, volume, funding_rate,
                open_interest_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                currency = excluded.currency,
                display_last = excluded.display_last,
                display_previous_close = excluded.display_previous_close,
                display_change_abs = excluded.display_change_abs,
                display_change_pct = excluded.display_change_pct,
                display_currency = excluded.display_currency,
                volume = excluded.volume,
                funding_rate = excluded.funding_rate,
                open_interest_usd = excluded.open_interest_usd
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
                    quote.display_last,
                    quote.display_previous_close,
                    quote.display_change_abs,
                    quote.display_change_pct,
                    quote.display_currency,
                    quote.volume,
                    quote.funding_rate,
                    quote.open_interest_usd,
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
                   timestamp, is_stale, error, currency, display_last, display_previous_close,
                   display_change_abs, display_change_pct, display_currency, volume,
                   funding_rate, open_interest_usd
            FROM latest_quotes
            WHERE symbol = ?
            """,
            (symbol.upper(),),
        ).fetchone()
    if row is None:
        return None
    return _quote_from_row(row)


def load_latest_quotes(path: Path, symbols: Sequence[str]) -> dict[str, Quote]:
    """Load cached quotes for normalized symbols in bounded batch queries."""
    normalized = sorted({symbol.upper() for symbol in symbols})
    if not normalized:
        return {}

    init_db(path)
    quotes: dict[str, Quote] = {}
    with _connect(path) as conn:
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset : offset + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT symbol, asset_type, provider, last, previous_close, change_abs,
                       change_pct, timestamp, is_stale, error, currency, display_last,
                       display_previous_close, display_change_abs, display_change_pct,
                       display_currency, volume, funding_rate, open_interest_usd
                FROM latest_quotes
                WHERE UPPER(symbol) IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                quote = _quote_from_row(row)
                quotes[quote.symbol.upper()] = quote
    return quotes


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
    """Load the newest bars per symbol/provider, with each series ascending."""
    if limit_per_series <= 0:
        raise ValueError("limit_per_series must be positive")

    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT symbol, provider, interval, timestamp, open, high, low, close, volume,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol, provider
                           ORDER BY timestamp DESC
                       ) AS series_row
                FROM bars
                WHERE interval = ?
            )
            SELECT symbol, provider, interval, timestamp, open, high, low, close, volume
            FROM ranked
            WHERE series_row <= ?
            ORDER BY symbol, provider, timestamp
            """,
            (interval, limit_per_series),
        ).fetchall()

    grouped: dict[tuple[str, ProviderName], list[Bar]] = {}
    for row in rows:
        bar = _bar_from_row(row)
        grouped.setdefault((bar.symbol, bar.provider), []).append(bar)
    return grouped


def newest_bar_timestamps(path: Path, interval: str) -> dict[str, datetime]:
    """Newest bar timestamp per symbol for one interval (any provider)."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT symbol, MAX(timestamp) AS newest FROM bars WHERE interval = ? GROUP BY symbol",
            (interval,),
        ).fetchall()
    return {str(row["symbol"]): _from_iso(str(row["newest"])) for row in rows if row["newest"]}


def save_board_snapshot(path: Path, snapshot_date: str, payload: dict[str, object]) -> None:
    """Upsert one condensed daily-board snapshot keyed by UTC date."""
    init_db(path)
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO board_snapshots (snapshot_date, created_at, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                created_at = excluded.created_at,
                payload = excluded.payload
            """,
            (snapshot_date, _to_iso(datetime.now(UTC)), json.dumps(payload)),
        )


def load_board_snapshots(path: Path, limit: int) -> list[dict[str, object]]:
    """Snapshots for the most recent `limit` dates, oldest first."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT snapshot_date, payload FROM board_snapshots"
            " ORDER BY snapshot_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    snapshots: list[dict[str, object]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(str(row["payload"]))
        except ValueError:
            continue
        if isinstance(payload, dict):
            payload["date"] = str(row["snapshot_date"])
            snapshots.append(payload)
    return snapshots


def save_report(path: Path, *, slug: str, report_date: str, title: str, body: str) -> int:
    """Upsert one agent report; only the NEWEST date per slug survives.

    Same-day cron re-runs replace that day's report; a new day's brief
    replaces the previous day's entirely. Pruning by MAX(report_date)
    (not the incoming date) means a late edit to an older vault file can
    never displace a newer brief. Delete + insert share one transaction.
    """
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            """
            INSERT INTO reports (slug, report_date, title, body, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug, report_date) DO UPDATE SET
                title = excluded.title,
                body = excluded.body,
                created_at = excluded.created_at
            RETURNING id
            """,
            (slug, report_date, title, body, _to_iso(datetime.now(UTC))),
        ).fetchone()
        conn.execute(
            """
            DELETE FROM reports
            WHERE slug = ?
              AND report_date < (SELECT MAX(report_date) FROM reports WHERE slug = ?)
            """,
            (slug, slug),
        )
    return int(row["id"])


def load_reports(path: Path, limit: int) -> list[dict[str, object]]:
    """Report metadata plus a short plain-text preview, newest first."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, slug, report_date, title, substr(body, 1, 16384) AS body, created_at"
            " FROM reports ORDER BY report_date DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "slug": str(row["slug"]),
            "date": str(row["report_date"]),
            "title": str(row["title"]),
            "created_at": str(row["created_at"]),
            "preview": _report_preview(str(row["body"])),
        }
        for row in rows
    ]


def load_report(path: Path, report_id: int) -> dict[str, object] | None:
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT id, slug, report_date, title, body, created_at FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "slug": str(row["slug"]),
        "date": str(row["report_date"]),
        "title": str(row["title"]),
        "created_at": str(row["created_at"]),
        "body": str(row["body"]),
    }


def delete_report(path: Path, report_id: int) -> bool:
    init_db(path)
    with _connect(path) as conn:
        cursor = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        return cursor.rowcount > 0


_MD_NOISE = str.maketrans({"#": None, "*": None, "`": None, ">": None, "|": None, "_": None})


def _strip_frontmatter(body: str) -> str:
    """Drop a leading Obsidian/Jekyll YAML block (--- ... ---)."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return body
    for index in range(1, min(len(lines), 40)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :])
    return body


def _report_preview(body: str, limit: int = 220) -> str:
    stripped = _strip_frontmatter(body)
    lines = [line.strip().translate(_MD_NOISE).strip() for line in stripped.splitlines()]
    text = " ".join(line for line in lines if line)
    return text[:limit].rstrip() + ("\u2026" if len(text) > limit else "")


def mark_stale(quote: Quote, *, error: str | None = None) -> Quote:
    return replace(quote, is_stale=True, error=error or quote.error)


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    # timeout=30: quote persistence, the bar heal, and report POSTs write
    # from separate to_thread workers; the 5s default surfaced contention
    # as "database is locked" and the write was lost.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets readers proceed alongside the single writer. The mode
    # persists in the file, so re-running the pragma here is a cheap no-op.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        # sqlite3's own context manager commits/rolls back but never
        # closes; every call-site's `with` was leaking a connection.
        with conn:
            yield conn
    finally:
        conn.close()


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
        display_last=_optional_float(row["display_last"]),
        display_previous_close=_optional_float(row["display_previous_close"]),
        display_change_abs=_optional_float(row["display_change_abs"]),
        display_change_pct=_optional_float(row["display_change_pct"]),
        display_currency=cast(str | None, row["display_currency"]),
        volume=_optional_float(row["volume"]),
        funding_rate=_optional_float(row["funding_rate"]),
        open_interest_usd=_optional_float(row["open_interest_usd"]),
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
    return float(cast(str | bytes | SupportsFloat | SupportsIndex, value))
