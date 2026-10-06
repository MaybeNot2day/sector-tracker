from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from secrets import randbits

# Version 1 adopts pre-versioned databases without rebuilding their tables.
SCHEMA_VERSION = 2

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
    updated_at TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS ai_model_snapshots (
    snapshot_date TEXT NOT NULL,
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    input_price_per_million REAL NOT NULL,
    output_price_per_million REAL NOT NULL,
    cache_read_price_per_million REAL,
    blended_price_per_million REAL NOT NULL,
    context_length INTEGER,
    is_open_weight INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, model_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_model_snapshots_model_date
ON ai_model_snapshots (model_id, snapshot_date DESC);

CREATE TABLE IF NOT EXISTS ai_token_index (
    index_date TEXT PRIMARY KEY,
    index_price REAL NOT NULL,
    open_price REAL,
    proprietary_price REAL,
    frontier_price REAL,
    china_price REAL,
    total_tokens INTEGER NOT NULL,
    priced_tokens INTEGER NOT NULL,
    coverage_pct REAL NOT NULL,
    open_share_pct REAL NOT NULL,
    model_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_capex_history (
    symbol TEXT NOT NULL,
    period_end TEXT NOT NULL,
    capex REAL NOT NULL,
    revenue REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, period_end)
);

CREATE INDEX IF NOT EXISTS idx_ai_capex_history_symbol_date
ON ai_capex_history (symbol, period_end DESC);

CREATE TABLE IF NOT EXISTS ai_gpu_compute_snapshots (
    snapshot_date TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hyperliquid_listings (
    market_kind TEXT NOT NULL CHECK (market_kind IN ('crypto', 'xyz')),
    symbol TEXT NOT NULL,
    coin TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    auto_added INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (market_kind, symbol)
);

CREATE INDEX IF NOT EXISTS idx_hyperliquid_listings_active_seen
ON hyperliquid_listings (market_kind, auto_added, is_active, first_seen_at DESC);

CREATE TABLE IF NOT EXISTS sofr_history (
    effective_date TEXT PRIMARY KEY,
    rate REAL NOT NULL,
    percentile_1 REAL,
    percentile_25 REAL,
    percentile_75 REAL,
    percentile_99 REAL,
    volume_billions REAL,
    revision_indicator TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);
"""
VALID_BAR_SQL = """
    open > 0 AND high > 0 AND low > 0 AND close > 0
    AND open < 1.0e100 AND high < 1.0e100 AND low < 1.0e100 AND close < 1.0e100
    AND high >= open AND high >= close
    AND low <= open AND low <= close
    AND low <= high
"""


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Apply ordered migrations under one cross-process SQLite writer lock."""
    conn.execute("BEGIN IMMEDIATE")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
    if version < 1:
        _adopt_legacy_schema(conn)
        conn.execute("PRAGMA user_version = 1")
    if version < 2:
        _add_history_metadata(conn)
        conn.execute("PRAGMA user_version = 2")
    # Also quarantine corrupt rows introduced outside the provider path. The
    # delete trigger participates in this transaction and invalidates readers.
    _quarantine_invalid_bars(conn)


def _adopt_legacy_schema(conn: sqlite3.Connection) -> None:
    # executescript commits before executing: execute each schema statement to
    # keep schema, data backfills and version advancement atomic instead.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    for column, definition in (
        ("currency", "TEXT"),
        ("display_last", "REAL"),
        ("display_previous_close", "REAL"),
        ("display_change_abs", "REAL"),
        ("display_change_pct", "REAL"),
        ("display_currency", "TEXT"),
        ("volume", "REAL"),
        ("funding_rate", "REAL"),
        ("open_interest_usd", "REAL"),
    ):
        _ensure_column(conn, "latest_quotes", column, definition)
    _ensure_column(conn, "reports", "updated_at", "TEXT")
    conn.execute("UPDATE reports SET updated_at = created_at WHERE updated_at IS NULL")
    for column, definition in (
        ("target", "TEXT"),
        ("confidence", "REAL"),
        ("stop", "TEXT"),
        ("mae_pct", "REAL"),
        ("mfe_pct", "REAL"),
    ):
        _ensure_column(conn, "fringe_ideas", column, definition)
    if _ensure_column(conn, "fringe_ideas", "size_notional", "REAL"):
        conn.execute(
            "UPDATE fringe_ideas SET size_notional = 1000.0"
            " WHERE status = 'open' AND size_notional IS NULL"
        )
    conn.execute(
        "UPDATE fringe_ideas SET size_notional = 1000.0"
        " WHERE status = 'closed' AND size_notional IS NULL"
        " AND entry_price IS NOT NULL AND exit_price IS NOT NULL"
    )
    _seed_key_date_sources(conn)
    _ensure_column(conn, "ai_token_index", "frontier_price", "REAL")
    _ensure_column(conn, "ai_token_index", "china_price", "REAL")


def _add_history_metadata(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE bars_state (id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL)"
    )
    # A per-file epoch also invalidates a service cache when the database file
    # is replaced by a new initialized database with the same number of bars.
    conn.execute("INSERT INTO bars_state VALUES (1, ?)", (randbits(62),))
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER bars_revision_{operation.lower()} AFTER {operation} ON bars "
            "BEGIN UPDATE bars_state SET revision = revision + 1 WHERE id = 1; END"
        )
    conn.execute(
        "CREATE TABLE bar_refreshes ("
        "symbol TEXT NOT NULL, provider TEXT NOT NULL, interval TEXT NOT NULL,"
        "fetched_at TEXT NOT NULL, newest_timestamp TEXT NOT NULL,"
        "PRIMARY KEY (symbol, provider, interval))"
    )


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
        WHERE NOT ({VALID_BAR_SQL})
        """,
        (quarantined_at,),
    )
    conn.execute(f"DELETE FROM bars WHERE NOT ({VALID_BAR_SQL})")


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


