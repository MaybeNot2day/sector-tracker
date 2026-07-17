"""Fringe Corner: grammar, ledger reconcile, P&L, routes, market context.

The ledger is an accruing book Hermes manages with explicit OPEN/HOLD/CLOSE
bullets — NOT a mirror like key_dates. These tests pin the reconcile
semantics (same-day retraction only), the price-stamping contract (null on
provider failure, lazily re-stamped), and the /api/fringe and
/api/market-context payload shapes the frontend and Hermes build against.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from app import db
from app.main import app
from app.models import AssetConfig, Bar, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider
from app.services.crypto_etf_flows import CryptoEtfFlowService
from app.services.fringe import FringeService, _pnl_pct, parse_fringe_actions

# --- grammar ---------------------------------------------------------------


def test_parse_all_three_actions_and_separators() -> None:
    body = """# Daily Brief

## Fringe Corner

- OPEN LONG CIFR — miner squeeze setup [horizon: 2w]
- HOLD SHORT XLU - utilities crowded into the print
- CLOSE LONG NVDA: earnings played out
"""
    actions = parse_fringe_actions(body)
    assert actions is not None
    assert [(a.action, a.direction, a.ticker) for a in actions] == [
        ("open", "long", "CIFR"),
        ("hold", "short", "XLU"),
        ("close", "long", "NVDA"),
    ]
    assert actions[0].text == "miner squeeze setup"
    assert actions[0].horizon == "2w"
    assert actions[1].horizon is None
    assert actions[2].text == "earnings played out"


def test_parse_actions_are_case_insensitive_but_tickers_stay_uppercase() -> None:
    body = "## fringe ideas\n- open short BRK-B — hedge [horizon: into Q3]\n"
    actions = parse_fringe_actions(body)
    assert actions is not None
    (action,) = actions
    assert (action.action, action.direction, action.ticker) == ("open", "short", "BRK-B")
    assert action.horizon == "into Q3"


def test_parse_skips_malformed_bullets_without_failing() -> None:
    body = """## Fringe Corner

- OPEN LONG cifr — lowercase ticker is prose, not a symbol
- OPEN SIDEWAYS TSLA — unknown direction
- HOLD LONG — missing ticker
- watching the tape, nothing actionable
- CLOSE SHORT ES=F — futures roll done
"""
    actions = parse_fringe_actions(body)
    assert actions is not None
    assert [(a.action, a.ticker) for a in actions] == [("close", "ES=F")]


def test_parse_ignores_bullets_outside_fringe_sections() -> None:
    body = """## Overnight

- OPEN LONG SPY — this is commentary, not a fringe idea

## Fringe Corner

- OPEN LONG CIFR — real idea

## Key Dates

- OPEN LONG QQQ — also not fringe
"""
    actions = parse_fringe_actions(body)
    assert actions is not None
    assert [a.ticker for a in actions] == ["CIFR"]


def test_parse_returns_none_without_a_fringe_heading_and_empty_for_empty_section() -> None:
    # None: the ledger must stay untouched (no same-day retraction).
    assert parse_fringe_actions("## Overnight\n- OPEN LONG SPY — x\n") is None
    # Empty list: the section exists and asserts "no ideas today".
    assert parse_fringe_actions("## Fringe Corner\n\nNothing today.\n") == []


# --- ledger reconcile (db) --------------------------------------------------


def act(
    action: str, ticker: str, direction: str, text: str = "t", horizon: str | None = None
) -> tuple[str, str, str, str, str | None]:
    return (action, ticker, direction, text, horizon)


def open_ideas(path: Path) -> list[dict[str, object]]:
    return db.load_fringe_ideas(path, status="open")


def test_open_then_hold_updates_thesis_and_last_mentioned(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-15",
        actions=[act("open", "CIFR", "long", "initial thesis", "2w")],
    )
    (idea,) = open_ideas(path)
    db.stamp_fringe_prices(path, entries=[(int(str(idea["id"])), 8.42)])

    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16",
        actions=[act("hold", "CIFR", "long", "still working")],
    )

    (idea,) = open_ideas(path)
    assert idea["thesis"] == "still working"
    assert idea["last_mentioned"] == "2026-07-16"
    # HOLD keeps what it does not restate; OPEN facts are preserved.
    assert idea["horizon"] == "2w"
    assert idea["opened_date"] == "2026-07-15"
    assert idea["entry_price"] == 8.42


def test_reopen_is_idempotent_and_preserves_entry_and_opened_date(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-15",
        actions=[act("open", "CIFR", "long", "v1", "2w")],
    )
    (idea,) = open_ideas(path)
    db.stamp_fringe_prices(path, entries=[(int(str(idea["id"])), 8.42)])

    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16",
        actions=[act("open", "CIFR", "long", "v2", "1m")],
    )

    (idea,) = open_ideas(path)  # still exactly one open idea
    assert (idea["thesis"], idea["horizon"]) == ("v2", "1m")
    assert idea["opened_date"] == "2026-07-15"
    assert idea["entry_price"] == 8.42


def test_close_stamps_date_and_reason_and_close_without_open_is_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "board.sqlite3"
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-15",
        actions=[act("open", "NVDA", "long", "run into earnings")],
    )
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16",
        actions=[
            act("close", "NVDA", "long", "earnings played out"),
            act("close", "TSLA", "short", "never existed"),
        ],
    )

    assert open_ideas(path) == []
    (closed,) = db.load_fringe_ideas(path, status="closed")
    assert closed["ticker"] == "NVDA"
    assert closed["closed_date"] == "2026-07-16"
    assert closed["close_reason"] == "earnings played out"
    assert closed["exit_price"] is None  # stamped separately, best-effort


def test_hold_without_open_idea_opens_one(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16",
        actions=[act("hold", "XLU", "short", "crowded", "1w")],
    )
    (idea,) = open_ideas(path)
    assert (idea["ticker"], idea["direction"], idea["opened_date"]) == (
        "XLU", "short", "2026-07-16",
    )


def test_same_ticker_opposite_directions_are_separate_ideas(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16",
        actions=[act("open", "GLD", "long"), act("open", "GLD", "short")],
    )
    assert sorted(str(idea["direction"]) for idea in open_ideas(path)) == ["long", "short"]


def test_same_day_rerun_retracts_only_that_days_absent_creations(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    # Day 1 opens A; day 2 opens B and C.
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-15", actions=[act("open", "AAA", "long")]
    )
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16",
        actions=[act("open", "BBB", "long"), act("open", "CCC", "long")],
    )

    # Day-2 re-run no longer mentions C: C never really existed. A (prior
    # day) and B (still mentioned) must survive.
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16", actions=[act("open", "BBB", "long")]
    )

    assert [idea["ticker"] for idea in open_ideas(path)] == ["AAA", "BBB"]


def test_rerun_of_a_different_slug_never_retracts_anothers_ideas(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16", actions=[act("open", "AAA", "long")]
    )
    db.apply_fringe_actions(path, slug="other-brief", report_date="2026-07-16", actions=[])
    assert [idea["ticker"] for idea in open_ideas(path)] == ["AAA"]


# --- P&L math ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "entry", "last", "expected"),
    [
        ("long", 8.42, 8.77, 4.16),
        ("long", 100.0, 90.0, -10.0),
        ("short", 100.0, 90.0, 10.0),
        ("short", 160.2, 173.1, -8.05),
        ("long", None, 90.0, None),
        ("long", 100.0, None, None),
        ("long", 0.0, 90.0, None),  # zero entry cannot divide
    ],
)
def test_pnl_pct_long_short_and_null(
    direction: str, entry: float | None, last: float | None, expected: float | None
) -> None:
    assert _pnl_pct(direction, entry, last) == expected


# --- providers stubs ---------------------------------------------------------


def make_quote(asset: AssetConfig, provider: ProviderName, last: float) -> Quote:
    return Quote(
        symbol=asset.symbol,
        asset_type=asset.type,
        provider=provider,
        last=last,
        previous_close=None,
        change_abs=None,
        change_pct=None,
        timestamp=datetime.now(UTC),
    )


class ScriptedYahoo(QuoteProvider):
    name: ProviderName = "yahoo"

    def __init__(self, prices: dict[str, float], *, fail: bool = False) -> None:
        self.prices = dict(prices)
        self.fail = fail
        self.requested: list[AssetConfig] = []

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        self.requested.extend(assets)
        if self.fail:
            raise RuntimeError("yahoo down")
        return [
            make_quote(asset, self.name, self.prices[asset.symbol])
            for asset in assets
            if asset.symbol in self.prices
        ]

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return []


class ScriptedLighter(LighterProvider):
    """Real LighterProvider (isinstance gates the routing) with a warm cache."""

    def __init__(self, prices: dict[str, float]) -> None:
        super().__init__()
        self.prices = dict(prices)
        self.requested: list[AssetConfig] = []
        # strategy_index 2 = crypto perp bucket (see _is_crypto_detail).
        self._details = {
            symbol: {"symbol": symbol, "market_id": index + 1, "status": "active",
                     "strategy_index": 2}
            for index, symbol in enumerate(sorted(prices))
        }
        self._details_time = monotonic()

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        self.requested.extend(assets)
        return [
            make_quote(asset, "lighter", self.prices[asset.symbol])
            for asset in assets
            if asset.symbol in self.prices
        ]


# --- /api/fringe route --------------------------------------------------------


@pytest.fixture
def configure_app(tmp_path: Path) -> Iterator[Callable[..., Path]]:
    """Stub settings + fringe service on app.state; restore afterwards."""
    saved = {
        name: getattr(app.state, name)
        for name in ("settings", "fringe_service", "groups", "econ_calendar_service")
        if hasattr(app.state, name)
    }

    def _configure(
        providers: dict[ProviderName, QuoteProvider] | None = None,
        groups: list[GroupConfig] | None = None,
    ) -> Path:
        path = tmp_path / "board.sqlite3"
        app.state.settings = SimpleNamespace(edit_token="", database_path=path)
        app.state.fringe_service = FringeService(path, providers or {})
        app.state.groups = groups or []
        app.state.econ_calendar_service = None
        return path

    yield _configure

    for name in ("settings", "fringe_service", "groups", "econ_calendar_service"):
        if name in saved:
            setattr(app.state, name, saved[name])
        elif hasattr(app.state, name):
            delattr(app.state, name)


FRINGE_REPORT = {
    "title": "Hermes Fringe Corner",
    "date": "2026-07-16",
    "body": """## Fringe Corner

- OPEN LONG CIFR — miner squeeze setup [horizon: 2w]
- OPEN LONG BTC — flows turning
""",
}


def test_fringe_route_stamps_entries_and_routes_lighter_vs_yahoo(
    configure_app: Callable[..., Path],
) -> None:
    yahoo = ScriptedYahoo({"CIFR": 8.42})
    lighter = ScriptedLighter({"BTC": 60000.0})
    configure_app({"yahoo": yahoo, "lighter": lighter})
    client = TestClient(app)

    created = client.post("/api/reports", json=FRINGE_REPORT)
    assert created.status_code == 200
    assert created.json()["fringe_actions"] == 2

    # Crypto tickers Lighter lists go to Lighter as crypto perps; anything
    # else is a Yahoo equity. Entry prices are stamped at ingest.
    assert [(a.symbol, a.type, a.source) for a in lighter.requested] == [
        ("BTC", "crypto_perp", "lighter")
    ]
    assert [(a.symbol, a.type, a.source) for a in yahoo.requested] == [
        ("CIFR", "equity", "yahoo")
    ]

    # Mark-to-market on a later build: bust the quote TTL and move the tape.
    yahoo.prices["CIFR"] = 8.77
    app.state.fringe_service.QUOTE_TTL_SECONDS = 0.0
    payload = client.get("/api/fringe").json()

    by_ticker = {item["ticker"]: item for item in payload["open"]}
    cifr = by_ticker["CIFR"]
    assert cifr["entry_price"] == 8.42
    assert cifr["last"] == 8.77
    assert cifr["unrealized_pct"] == 4.16
    assert cifr["horizon"] == "2w"
    assert cifr["opened"] == "2026-07-16"
    assert cifr["stale"] is False
    assert cifr["source_slug"] == "hermes-fringe-corner"
    assert by_ticker["BTC"]["entry_price"] == 60000.0
    assert payload["closed"] == []


def test_fringe_route_lazily_restamps_after_failed_ingest_stamp(
    configure_app: Callable[..., Path],
) -> None:
    yahoo = ScriptedYahoo({"CIFR": 8.42}, fail=True)
    configure_app({"yahoo": yahoo})
    client = TestClient(app)

    assert client.post("/api/reports", json=FRINGE_REPORT).status_code == 200

    # Provider still down: prices stay null, the route still answers.
    first = client.get("/api/fringe").json()
    by_ticker = {item["ticker"]: item for item in first["open"]}
    assert by_ticker["CIFR"]["entry_price"] is None
    assert by_ticker["CIFR"]["last"] is None
    assert by_ticker["CIFR"]["unrealized_pct"] is None

    # Provider recovers: the next build stamps the missing entry price.
    yahoo.fail = False
    second = client.get("/api/fringe").json()
    by_ticker = {item["ticker"]: item for item in second["open"]}
    assert by_ticker["CIFR"]["entry_price"] == 8.42
    # The stamp is persisted, not just reflected in the payload.
    stored = {
        idea["ticker"]: idea["entry_price"]
        for idea in db.load_fringe_ideas(app.state.settings.database_path, status="open")
    }
    assert stored == {"CIFR": 8.42, "BTC": None}  # BTC has no quote source here


def test_fringe_route_flags_ideas_the_newest_report_did_not_refresh(
    configure_app: Callable[..., Path],
) -> None:
    path = configure_app({})
    client = TestClient(app)
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-15", actions=[act("open", "AAA", "long")]
    )
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16", actions=[act("hold", "BBB", "long")]
    )

    payload = client.get("/api/fringe").json()

    stale = {item["ticker"]: item["stale"] for item in payload["open"]}
    assert stale == {"AAA": True, "BBB": False}


def test_close_stamps_exit_price_and_realized_pnl(
    configure_app: Callable[..., Path],
) -> None:
    yahoo = ScriptedYahoo({"NVDA": 160.2})
    configure_app({"yahoo": yahoo})
    client = TestClient(app)

    opened = client.post(
        "/api/reports",
        json={
            "title": "Hermes Fringe Corner",
            "date": "2026-07-10",
            "body": "## Fringe Corner\n- OPEN LONG NVDA — into earnings\n",
        },
    )
    assert opened.status_code == 200

    yahoo.prices["NVDA"] = 173.1
    app.state.fringe_service.QUOTE_TTL_SECONDS = 0.0
    closed = client.post(
        "/api/reports",
        json={
            "title": "Hermes Fringe Corner",
            "date": "2026-07-16",
            "body": "## Fringe Corner\n- CLOSE LONG NVDA — earnings played out\n",
        },
    )
    assert closed.status_code == 200

    payload = client.get("/api/fringe").json()
    assert payload["open"] == []
    (item,) = payload["closed"]
    assert item["entry_price"] == 160.2
    assert item["exit_price"] == 173.1
    assert item["realized_pct"] == 8.05
    assert item["close_reason"] == "earnings played out"
    assert item["opened"] == "2026-07-10"
    assert item["closed"] == "2026-07-16"


# --- etf flow history ----------------------------------------------------------


def test_etf_flow_history_upsert_is_idempotent_and_range_loads(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    rows = [("BTC", "2026-07-14", 100.0), ("BTC", "2026-07-15", -50.0)]
    db.upsert_etf_flow_history(path, rows)
    db.upsert_etf_flow_history(path, rows)  # replay: no duplicates
    db.upsert_etf_flow_history(path, [("BTC", "2026-07-15", -75.0)])  # revision wins

    loaded = db.load_etf_flow_history(path, start="2026-07-01")
    assert loaded == {
        "BTC": [
            {"date": "2026-07-14", "flow": 100.0},
            {"date": "2026-07-15", "flow": -75.0},
        ]
    }
    # Ranged load drops rows before `start`.
    assert db.load_etf_flow_history(path, start="2026-07-15") == {
        "BTC": [{"date": "2026-07-15", "flow": -75.0}]
    }


@pytest.mark.asyncio
async def test_flow_service_persists_history_after_successful_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.sqlite3"
    service = CryptoEtfFlowService(database_path=path)
    fetched = [
        {
            "asset": "BTC",
            "name": "Bitcoin",
            "rows": [
                {"date": "2026-07-15", "flow_usd": 100_000_000},
                {"date": "2026-07-16", "flow_usd": -25_000_000},
            ],
        }
    ]
    monkeypatch.setattr(service, "_fetch_assets_sync", lambda: fetched)

    payload = await service.get_flows()

    assert payload["status"] == "ok"
    assert db.load_etf_flow_history(path, start="2026-07-01") == {
        "BTC": [
            {"date": "2026-07-15", "flow": 100_000_000.0},
            {"date": "2026-07-16", "flow": -25_000_000.0},
        ]
    }


@pytest.mark.asyncio
async def test_flow_service_survives_history_persist_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CryptoEtfFlowService(database_path=tmp_path / "board.sqlite3")
    fetched = [{"asset": "BTC", "name": "Bitcoin", "rows": [{"date": "2026-07-15", "flow_usd": 1}]}]
    monkeypatch.setattr(service, "_fetch_assets_sync", lambda: fetched)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "upsert_etf_flow_history", explode)

    payload = await service.get_flows()

    assert payload["status"] == "ok"
    assert [entry["asset"] for entry in payload["assets"]] == ["BTC"]


# --- /api/market-context ---------------------------------------------------------


def daily_bars(symbol: str, closes: list[float]) -> list[Bar]:
    start = datetime(2026, 6, 20, tzinfo=UTC)
    return [
        Bar(
            symbol=symbol,
            provider="yahoo",
            interval="1d",
            timestamp=start + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
        )
        for index, close in enumerate(closes)
    ]


def test_market_context_shape_movers_and_clamped_days(
    configure_app: Callable[..., Path],
) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[
                AssetConfig(symbol="AAA", type="equity", source="yahoo"),
                AssetConfig(symbol="BBB", type="equity", source="yahoo"),
            ],
        )
    ]
    path = configure_app({}, groups=groups)
    client = TestClient(app)

    # 21 daily closes: AAA grinds up, BBB bleeds.
    db.save_bars(path, daily_bars("AAA", [100.0 + i for i in range(21)]))
    db.save_bars(path, daily_bars("BBB", [200.0 - 2 * i for i in range(21)]))
    db.save_board_snapshot(path, "2026-07-15", {"regime": "risk-on"})
    db.save_board_snapshot(path, "2026-07-16", {"regime": "chop"})
    db.upsert_etf_flow_history(path, [("BTC", "2026-07-16", 100_000_000.0)])
    tomorrow = (datetime.now(ZoneInfo("America/New_York")).date() + timedelta(days=1)).isoformat()
    db.replace_key_dates(path, slug="brief", events=[(tomorrow, "08:30 ET", "CPI", "MACRO")])
    db.apply_fringe_actions(
        path, slug="fringe", report_date="2026-07-16", actions=[act("open", "AAA", "long")]
    )

    payload = client.get("/api/market-context", params={"days": 500}).json()

    assert payload["days"] == 90  # clamped, not rejected: the caller is a bot
    assert set(payload) == {
        "as_of", "days", "snapshots", "movers", "etf_flows", "key_dates", "fringe_book",
    }
    # Snapshots are the exact rows /api/snapshots serves (oldest first, date-keyed).
    assert [row["date"] for row in payload["snapshots"]] == ["2026-07-15", "2026-07-16"]
    assert payload["snapshots"][1]["regime"] == "chop"
    # Movers from the cached daily closes: 120 vs 115 five bars back, etc.
    movers = payload["movers"]
    assert movers["5d"]["leaders"][0] == {"symbol": "AAA", "pct": 4.35}
    assert movers["5d"]["laggards"][0] == {"symbol": "BBB", "pct": -5.88}
    assert movers["20d"]["leaders"][0] == {"symbol": "AAA", "pct": 20.0}
    assert movers["20d"]["laggards"][0] == {"symbol": "BBB", "pct": -20.0}
    assert payload["etf_flows"] == {"BTC": [{"date": "2026-07-16", "flow": 100_000_000.0}]}
    assert [item["title"] for item in payload["key_dates"]] == ["CPI"]
    assert [idea["ticker"] for idea in payload["fringe_book"]["open"]] == ["AAA"]
    assert payload["fringe_book"]["recently_closed"] == []

    assert client.get("/api/market-context", params={"days": 1}).json()["days"] == 7


def test_market_context_degrades_to_empty_pieces_instead_of_500(
    configure_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_app({})
    client = TestClient(app)

    def explode(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("db gone")

    monkeypatch.setattr(db, "load_board_snapshots", explode)

    response = client.get("/api/market-context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshots"] == []
    assert payload["fringe_book"] == {"open": [], "recently_closed": []}
