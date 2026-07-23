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

from app.models import AssetType, Bar, ProviderName, Quote, is_valid_bar

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

CREATE TABLE IF NOT EXISTS invalid_bars (
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    quarantined_at TEXT NOT NULL,
    reason TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS key_dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    event_time TEXT,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    source_slug TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (event_date, title)
);

CREATE INDEX IF NOT EXISTS idx_key_dates_slug ON key_dates (source_slug);

CREATE TABLE IF NOT EXISTS key_date_sources (
    source_slug TEXT NOT NULL,
    event_date TEXT NOT NULL,
    event_time TEXT,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_slug, event_date, title)
);

CREATE INDEX IF NOT EXISTS idx_key_date_sources_event
ON key_date_sources (event_date, title, created_at DESC);

CREATE TABLE IF NOT EXISTS fringe_ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    thesis TEXT NOT NULL,
    horizon TEXT,
    target TEXT,
    confidence REAL,
    stop TEXT,
    size_notional REAL,
    status TEXT NOT NULL DEFAULT 'open',
    opened_date TEXT NOT NULL,
    closed_date TEXT,
    close_reason TEXT,
    entry_price REAL,
    exit_price REAL,
    last_mentioned TEXT NOT NULL,
    source_slug TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fringe_ideas_status ON fringe_ideas (status, ticker, direction);

CREATE TABLE IF NOT EXISTS fringe_equity_history (
    date TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    realized_usd REAL NOT NULL,
    unrealized_usd REAL NOT NULL,
    invested_notional REAL NOT NULL,
    open_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etf_flow_history (
    asset TEXT NOT NULL,
    flow_date TEXT NOT NULL,
    flow REAL NOT NULL,
    PRIMARY KEY (asset, flow_date)
);
"""
_initialized_paths: set[Path] = set()
_init_lock = Lock()

_VALID_BAR_SQL = """
    open > 0 AND high > 0 AND low > 0 AND close > 0
    AND open < 1.0e100 AND high < 1.0e100 AND low < 1.0e100 AND close < 1.0e100
    AND high >= open AND high >= close
    AND low <= open AND low <= close
    AND low <= high
"""



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
            _ensure_column(conn, "fringe_ideas", "target", "TEXT")
            _ensure_column(conn, "fringe_ideas", "confidence", "REAL")
            _ensure_column(conn, "fringe_ideas", "stop", "TEXT")
            sized_added = _ensure_column(conn, "fringe_ideas", "size_notional", "REAL")
            if sized_added:
                # Pre-capital open ideas are grandfathered at a flat $1,000
                # of the $10k paper book.
                conn.execute(
                    "UPDATE fringe_ideas SET size_notional = 1000.0"
                    " WHERE status = 'open' AND size_notional IS NULL"
                )
            # Pre-capital CLOSES get the same flat $1,000 so their realized
            # dollars enter the bankroll. Safe to replay forever: under the
            # sizing regime a position is sized in the same pass that stamps
            # its entry, so "closed + priced + unsized" can only describe
            # rows that predate the capital era.
            conn.execute(
                "UPDATE fringe_ideas SET size_notional = 1000.0"
                " WHERE status = 'closed' AND size_notional IS NULL"
                " AND entry_price IS NOT NULL AND exit_price IS NOT NULL"
            )
            _seed_key_date_sources(conn)
            _quarantine_invalid_bars(conn)
        _initialized_paths.add(resolved)


def _seed_key_date_sources(conn: sqlite3.Connection) -> None:
    """Preserve pre-migration calendar ownership as the first attribution."""
    conn.execute(
        """
        INSERT OR IGNORE INTO key_date_sources (
            source_slug, event_date, event_time, title, category, created_at
        )
        SELECT source_slug, event_date, event_time, title, category, created_at
        FROM key_dates
        """
    )


def _quarantine_invalid_bars(conn: sqlite3.Connection) -> None:
    """Move corrupt provider candles out of every downstream calculation."""
    quarantined_at = datetime.now(UTC).isoformat()
    conn.execute(
        f"""
        INSERT OR REPLACE INTO invalid_bars (
            symbol, provider, interval, timestamp, open, high, low, close, volume,
            quarantined_at, reason
        )
        SELECT symbol, provider, interval, timestamp, open, high, low, close, volume,
               ?, 'invalid_ohlc'
        FROM bars
        WHERE NOT ({_VALID_BAR_SQL})
        """,
        (quarantined_at,),
    )
    conn.execute(f"DELETE FROM bars WHERE NOT ({_VALID_BAR_SQL})")


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
    valid_bars = [bar for bar in bars if is_valid_bar(bar)]
    invalid_bars = [bar for bar in bars if not is_valid_bar(bar)]
    init_db(path)
    with _connect(path) as conn:
        if invalid_bars:
            conn.executemany(
                """
                INSERT INTO invalid_bars (
                    symbol, provider, interval, timestamp, open, high, low, close,
                    volume, quarantined_at, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, provider, interval, timestamp) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    quarantined_at = excluded.quarantined_at,
                    reason = excluded.reason
                """,
                [
                    (
                        bar.symbol,
                        bar.provider,
                        bar.interval,
                        _to_iso(bar.timestamp),
                        str(bar.open),
                        str(bar.high),
                        str(bar.low),
                        str(bar.close),
                        bar.volume,
                        _to_iso(datetime.now(UTC)),
                        "invalid_ohlc",
                    )
                    for bar in invalid_bars
                ],
            )
        if valid_bars:
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
                    for bar in valid_bars
                ],
            )
            conn.executemany(
                """
                DELETE FROM invalid_bars
                WHERE symbol = ? AND provider = ? AND interval = ? AND timestamp = ?
                """,
                [
                    (bar.symbol, bar.provider, bar.interval, _to_iso(bar.timestamp))
                    for bar in valid_bars
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
    """Upsert one agent report, ignoring stale dates older than the current brief."""
    init_db(path)
    with _connect(path) as conn:
        report_id, _ = _save_report(
            conn,
            slug=slug,
            report_date=report_date,
            title=title,
            body=body,
        )
    return report_id


def _save_report(
    conn: sqlite3.Connection,
    *,
    slug: str,
    report_date: str,
    title: str,
    body: str,
) -> tuple[int, bool]:
    latest = conn.execute(
        "SELECT id, report_date FROM reports WHERE slug = ?"
        " ORDER BY report_date DESC LIMIT 1",
        (slug,),
    ).fetchone()
    if latest is not None and report_date < str(latest["report_date"]):
        return int(latest["id"]), False

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
        "DELETE FROM reports WHERE slug = ? AND report_date < ?",
        (slug, report_date),
    )
    return int(row["id"]), True


def ingest_report(
    path: Path,
    *,
    slug: str,
    report_date: str,
    title: str,
    body: str,
    events: Sequence[tuple[str, str | None, str, str]],
    fringe_actions: Sequence[tuple[str, str, str, str, str | None, str | None]] | None,
) -> int:
    """Persist a report and every derived projection in one transaction."""
    init_db(path)
    with _connect(path) as conn:
        report_id, accepted = _save_report(
            conn,
            slug=slug,
            report_date=report_date,
            title=title,
            body=body,
        )
        if not accepted:
            return report_id
        _replace_key_dates(conn, slug=slug, events=events)
        if fringe_actions is not None:
            _apply_fringe_actions(
                conn,
                slug=slug,
                report_date=report_date,
                actions=fringe_actions,
            )
        return report_id


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
    """Remove one report and only its calendar attribution."""
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT slug FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return False
        slug = str(row["slug"])
        conn.execute("DELETE FROM key_date_sources WHERE source_slug = ?", (slug,))
        _rebuild_key_dates(conn)
        conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        return True


def replace_key_dates(
    path: Path, *, slug: str, events: Sequence[tuple[str, str | None, str, str]]
) -> int:
    """Mirror one report's calendar attribution without losing shared events."""
    init_db(path)
    with _connect(path) as conn:
        _replace_key_dates(conn, slug=slug, events=events)
    return len(events)


def _replace_key_dates(
    conn: sqlite3.Connection,
    *,
    slug: str,
    events: Sequence[tuple[str, str | None, str, str]],
) -> None:
    now = _to_iso(datetime.now(UTC))
    conn.execute("DELETE FROM key_date_sources WHERE source_slug = ?", (slug,))
    conn.executemany(
        """
        INSERT INTO key_date_sources (
            source_slug, event_date, event_time, title, category, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_slug, event_date, title) DO UPDATE SET
            event_time = excluded.event_time,
            category = excluded.category,
            created_at = excluded.created_at
        """,
        [(slug, date, time, title, category, now) for date, time, title, category in events],
    )
    _rebuild_key_dates(conn)


def _rebuild_key_dates(conn: sqlite3.Connection) -> None:
    """Project all source attributions into one newest-mention calendar row."""
    conn.execute(
        """
        DELETE FROM key_dates
        WHERE NOT EXISTS (
            SELECT 1
            FROM key_date_sources AS source
            WHERE source.event_date = key_dates.event_date
              AND source.title = key_dates.title
        )
        """
    )
    conn.execute(
        """
        INSERT INTO key_dates (
            event_date, event_time, title, category, source_slug, created_at
        )
        SELECT event_date, event_time, title, category, source_slug, created_at
        FROM (
            SELECT source.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY event_date, title
                       ORDER BY created_at DESC, source_slug DESC
                   ) AS source_rank
            FROM key_date_sources AS source
        )
        WHERE source_rank = 1
        ON CONFLICT(event_date, title) DO UPDATE SET
            event_time = excluded.event_time,
            category = excluded.category,
            source_slug = excluded.source_slug,
            created_at = excluded.created_at
        """
    )


def load_key_dates(path: Path, *, start: str, end: str, limit: int) -> list[dict[str, object]]:
    """Events with start <= date <= end, soonest first; NULL times sort last
    within a day (all-day items like unlocks trail timed prints)."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, event_date, event_time, title, category, source_slug
            FROM key_dates
            WHERE event_date >= ? AND event_date <= ?
            ORDER BY event_date, event_time IS NULL, event_time, title
            LIMIT ?
            """,
            (start, end, limit),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "date": str(row["event_date"]),
            "time": str(row["event_time"]) if row["event_time"] is not None else None,
            "title": str(row["title"]),
            "category": str(row["category"]),
            "source_slug": str(row["source_slug"]),
        }
        for row in rows
    ]


# --- Fringe Corner ideas ledger -------------------------------------------


def apply_fringe_actions(
    path: Path,
    *,
    slug: str,
    report_date: str,
    actions: Sequence[
        tuple[str, str, str, str, str | None, str | None, float | None, str | None]
    ],
) -> dict[str, int]:
    """Replay one report's fringe actions against the accruing ideas ledger."""
    init_db(path)
    with _connect(path) as conn:
        return _apply_fringe_actions(
            conn,
            slug=slug,
            report_date=report_date,
            actions=actions,
        )


def _apply_fringe_actions(
    conn: sqlite3.Connection,
    *,
    slug: str,
    report_date: str,
    actions: Sequence[
        tuple[str, str, str, str, str | None, str | None, float | None, str | None]
    ],
) -> dict[str, int]:
    now = _to_iso(datetime.now(UTC))
    counts = {"opened": 0, "updated": 0, "closed": 0, "removed": 0}
    mentioned: set[tuple[str, str]] = set()
    for action, ticker, direction, text, horizon, target, confidence, stop in actions:
        mentioned.add((ticker, direction))
        row = conn.execute(
            "SELECT id FROM fringe_ideas"
            " WHERE ticker = ? AND direction = ? AND status = 'open'",
            (ticker, direction),
        ).fetchone()
        if action == "close":
            if row is None:
                continue
            conn.execute(
                """
                UPDATE fringe_ideas
                SET status = 'closed', closed_date = ?, close_reason = ?,
                    last_mentioned = ?, updated_at = ?
                WHERE id = ?
                """,
                (report_date, text, report_date, now, int(row["id"])),
            )
            counts["closed"] += 1
        elif row is not None:
            # OPEN replaces optional terms; HOLD preserves omitted ones.
            restate = "?" if action == "open" else "COALESCE(?, {})"
            horizon_sql = restate.format("horizon")
            target_sql = restate.format("target")
            confidence_sql = restate.format("confidence")
            stop_sql = restate.format("stop")
            conn.execute(
                f"""
                UPDATE fringe_ideas
                SET thesis = ?, horizon = {horizon_sql}, target = {target_sql},
                    confidence = {confidence_sql}, stop = {stop_sql},
                    last_mentioned = ?, updated_at = ?
                WHERE id = ?
                """,
                (text, horizon, target, confidence, stop, report_date, now, int(row["id"])),
            )
            counts["updated"] += 1
        else:
            conn.execute(
                """
                INSERT INTO fringe_ideas (
                    ticker, direction, thesis, horizon, target, confidence,
                    stop, status, opened_date, last_mentioned, source_slug,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    direction,
                    text,
                    horizon,
                    target,
                    confidence,
                    stop,
                    report_date,
                    report_date,
                    slug,
                    now,
                    now,
                ),
            )
            counts["opened"] += 1

    # Same-day re-runs authoritatively retract ideas they opened that day.
    rows = conn.execute(
        "SELECT id, ticker, direction FROM fringe_ideas"
        " WHERE source_slug = ? AND opened_date = ? AND status = 'open'",
        (slug, report_date),
    ).fetchall()
    orphaned = [
        (int(row["id"]),)
        for row in rows
        if (str(row["ticker"]), str(row["direction"])) not in mentioned
    ]
    conn.executemany("DELETE FROM fringe_ideas WHERE id = ?", orphaned)
    counts["removed"] = len(orphaned)
    return counts


def load_fringe_ideas(
    path: Path, *, status: str, limit: int | None = None
) -> list[dict[str, object]]:
    """Ledger rows for one status: open ideas oldest first (stable panel
    order), closed ideas newest close first (recent history)."""
    init_db(path)
    order = "closed_date DESC, id DESC" if status == "closed" else "opened_date, id"
    params: list[object] = [status]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, ticker, direction, thesis, horizon, target, confidence,
                   stop, size_notional, status, opened_date, closed_date,
                   close_reason, entry_price, exit_price, last_mentioned,
                   source_slug
            FROM fringe_ideas
            WHERE status = ?
            ORDER BY {order}
            {limit_clause}
            """,
            params,
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "ticker": str(row["ticker"]),
            "direction": str(row["direction"]),
            "thesis": str(row["thesis"]),
            "horizon": str(row["horizon"]) if row["horizon"] is not None else None,
            "target": str(row["target"]) if row["target"] is not None else None,
            "confidence": _optional_float(row["confidence"]),
            "stop": str(row["stop"]) if row["stop"] is not None else None,
            "size_notional": _optional_float(row["size_notional"]),
            "status": str(row["status"]),
            "opened_date": str(row["opened_date"]),
            "closed_date": str(row["closed_date"]) if row["closed_date"] is not None else None,
            "close_reason": str(row["close_reason"]) if row["close_reason"] is not None else None,
            "entry_price": _optional_float(row["entry_price"]),
            "exit_price": _optional_float(row["exit_price"]),
            "last_mentioned": str(row["last_mentioned"]),
            "source_slug": str(row["source_slug"]),
        }
        for row in rows
    ]


def upsert_fringe_equity(
    path: Path,
    *,
    date_text: str,
    equity: float,
    realized_usd: float,
    unrealized_usd: float,
    invested_notional: float,
    open_count: int,
) -> None:
    """One mark-to-market equity point per day; same-day builds converge to
    the latest mark, so the stored row ends the day as the EOD snapshot."""
    init_db(path)
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO fringe_equity_history (
                date, equity, realized_usd, unrealized_usd,
                invested_notional, open_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                equity = excluded.equity,
                realized_usd = excluded.realized_usd,
                unrealized_usd = excluded.unrealized_usd,
                invested_notional = excluded.invested_notional,
                open_count = excluded.open_count,
                updated_at = excluded.updated_at
            """,
            (
                date_text,
                equity,
                realized_usd,
                unrealized_usd,
                invested_notional,
                open_count,
                _to_iso(datetime.now(UTC)),
            ),
        )


def load_fringe_equity(path: Path) -> list[dict[str, object]]:
    """Stored daily equity marks, oldest first."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT date, equity, realized_usd, unrealized_usd,"
            " invested_notional, open_count"
            " FROM fringe_equity_history ORDER BY date"
        ).fetchall()
    return [
        {
            "date": str(row["date"]),
            "equity": float(row["equity"]),
            "realized_usd": float(row["realized_usd"]),
            "unrealized_usd": float(row["unrealized_usd"]),
            "invested_notional": float(row["invested_notional"]),
            "open_count": int(row["open_count"]),
        }
        for row in rows
    ]


def set_fringe_sizes(path: Path, *, sizes: Sequence[tuple[int, float]]) -> None:
    """Persist notionals computed at entry stamp; a size is written once."""
    if not sizes:
        return
    init_db(path)
    now = _to_iso(datetime.now(UTC))
    with _connect(path) as conn:
        conn.executemany(
            "UPDATE fringe_ideas SET size_notional = ?, updated_at = ?"
            " WHERE id = ? AND size_notional IS NULL",
            [(notional, now, idea_id) for idea_id, notional in sizes],
        )


def close_fringe_idea(
    path: Path, *, idea_id: int, exit_price: float, closed_date: str, reason: str
) -> bool:
    """Close one open idea at a known mark — the intraday auto-stop path.

    Mirrors a report CLOSE (status, dates, reason) but stamps the exit price
    immediately: the caller just marked the ticker, no lazy re-stamp needed.
    """
    init_db(path)
    now = _to_iso(datetime.now(UTC))
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            UPDATE fringe_ideas
            SET status = 'closed', closed_date = ?, close_reason = ?,
                exit_price = ?, last_mentioned = ?, updated_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (closed_date, reason, exit_price, closed_date, now, idea_id),
        )
    return cursor.rowcount > 0


def latest_fringe_mention(path: Path) -> str | None:
    """Newest report date that fed the book; open ideas older than this
    were not refreshed by the latest report (the UI's `stale` flag)."""
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT MAX(last_mentioned) AS latest FROM fringe_ideas").fetchone()
    return str(row["latest"]) if row and row["latest"] is not None else None


def stamp_fringe_prices(
    path: Path,
    *,
    entries: Sequence[tuple[int, float]] = (),
    exits: Sequence[tuple[int, float]] = (),
) -> None:
    """Fill missing entry/exit prices by idea id; never overwrites a stamp.

    The IS NULL guard makes lazy re-stamping (a provider outage at ingest,
    retried on the next /api/fringe build) safe to call repeatedly.
    """
    if not entries and not exits:
        return
    init_db(path)
    now = _to_iso(datetime.now(UTC))
    with _connect(path) as conn:
        conn.executemany(
            "UPDATE fringe_ideas SET entry_price = ?, updated_at = ?"
            " WHERE id = ? AND entry_price IS NULL",
            [(price, now, idea_id) for idea_id, price in entries],
        )
        conn.executemany(
            "UPDATE fringe_ideas SET exit_price = ?, updated_at = ?"
            " WHERE id = ? AND exit_price IS NULL",
            [(price, now, idea_id) for idea_id, price in exits],
        )


# --- crypto ETF flow history ----------------------------------------------


def upsert_etf_flow_history(path: Path, rows: Sequence[tuple[str, str, float]]) -> None:
    """Accrue (asset, date, flow_usd) rows; a re-fetch updates in place.

    Farside serves only a ~20-day window, so this table is what gives the
    market-context digest flow history beyond the scrape horizon.
    """
    if not rows:
        return
    init_db(path)
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO etf_flow_history (asset, flow_date, flow)
            VALUES (?, ?, ?)
            ON CONFLICT(asset, flow_date) DO UPDATE SET flow = excluded.flow
            """,
            rows,
        )


def load_etf_flow_history(path: Path, *, start: str) -> dict[str, list[dict[str, object]]]:
    """Per-asset daily flows from `start` onward, ascending by date."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT asset, flow_date, flow FROM etf_flow_history"
            " WHERE flow_date >= ? ORDER BY asset, flow_date",
            (start,),
        ).fetchall()
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["asset"]), []).append(
            {"date": str(row["flow_date"]), "flow": float(row["flow"])}
        )
    return grouped


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
) -> bool:
    """Add a missing column; True when this call performed the migration."""
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


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
