from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from app import db
from app.config import (
    Settings,
    find_group,
    load_watchlists,
    save_watchlists,
    validate_watchlist_identities,
)
from app.models import AssetConfig, AssetType, GroupConfig, ProviderName
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider
from app.providers.stooq import StooqProvider
from app.providers.yahoo import YahooProvider
from app.scheduler import (
    ConnectionManager,
    board_payload_async,
    econ_calendar_loop,
    history_refresh_loop,
    news_poll_loop,
    quote_poll_loop,
    stop_task,
)
from app.services.asset_profile import AssetProfileService
from app.services.crypto_etf_flows import CryptoEtfFlowService
from app.services.daily_board import DailyBoardService
from app.services.econ_calendar import EconCalendarService, key_dates_payload
from app.services.fringe import FringeService, parse_fringe_actions
from app.services.history import HistoryService, bars_payload, find_asset
from app.services.key_dates import parse_key_dates
from app.services.macro import MACRO_TAPE_GROUP_NAME, with_macro_group
from app.services.market_context import market_context_payload
from app.services.news import NewsService
from app.services.quotes import QuoteService

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"
YAHOO_STATUS_CACHE_SECONDS = 60.0
HistoryInterval = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk", "1mo"]
HistoryRange = Literal["1d", "1w", "1mo", "3mo", "6mo", "ytd", "1y", "5y", "10y"]
_yahoo_status_cache: tuple[float, dict[str, object]] | None = None
_yahoo_status_lock = Lock()


logger = logging.getLogger(__name__)


class GroupRequest(BaseModel):
    # No `/` or `\`: uvicorn decodes %2F before routing, so a name with a
    # slash could never match the DELETE path param — undeletable forever.
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^/\\]+$")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        # min_length admits " "; clean_text collapses it to "", persisting an
        # empty-named group the DELETE path param can never match.
        if not clean_text(value):
            raise ValueError("name is blank")
        return value


class AssetRequest(BaseModel):
    # Same slash ban as GroupRequest.name, for the same DELETE-path reason.
    symbol: str = Field(min_length=1, max_length=24, pattern=r"^[^/\\]+$")
    type: AssetType = "equity"
    source: ProviderName = "yahoo"
    exchange: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=96)

    @field_validator("symbol")
    @classmethod
    def _symbol_not_blank(cls, value: str) -> str:
        # Same blank-collapse hole as GroupRequest.name: a " " symbol would
        # persist as "" and be undeletable via the DELETE path param.
        if not clean_symbol(value):
            raise ValueError("symbol is blank")
        return value


class ReportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=500_000)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    slug: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("date")
    @classmethod
    def _date_is_calendar(cls, value: str | None) -> str | None:
        # The regex admits non-calendar dates like 2025-02-31; reject them
        # here so a bad cron payload fails loudly instead of persisting.
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"not a real calendar date: {value}") from exc
        return value


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    ensure_runtime_watchlist(settings)
    ensure_runtime_database(settings)
    groups = load_watchlists(settings.watchlist_path)
    db.init_db(settings.database_path)

    providers: dict[ProviderName, QuoteProvider] = {
        "yahoo": YahooProvider(),
        "lighter": LighterProvider(),
        "stooq": StooqProvider(),
    }

    app.state.settings = settings
    app.state.groups = groups
    app.state.providers = providers
    app.state.quote_service = QuoteService(
        settings.database_path,
        providers,
        min_refresh_seconds=settings.quote_poll_seconds,
    )
    app.state.history_service = HistoryService(settings.database_path, providers)
    app.state.daily_board_service = DailyBoardService(settings.database_path)
    app.state.crypto_etf_flow_service = CryptoEtfFlowService(
        cache_seconds=settings.crypto_etf_flow_cache_seconds,
        database_path=settings.database_path,
    )
    app.state.fringe_service = FringeService(settings.database_path, providers)
    app.state.asset_profile_service = AssetProfileService()
    app.state.news_service = NewsService(
        settings.news_channels,
        cache_seconds=settings.news_poll_seconds,
    )
    app.state.econ_calendar_service = EconCalendarService(
        cache_seconds=settings.econ_calendar_cache_seconds,
        countries=settings.econ_calendar_countries,
    )
    app.state.connection_manager = ConnectionManager()
    app.state.watchlist_lock = asyncio.Lock()
    app.state.poll_task = None
    app.state.history_task = None
    app.state.news_task = None
    app.state.econ_calendar_task = None
    if settings.enable_background_tasks:
        app.state.poll_task = asyncio.create_task(quote_poll_loop(app.state))
        app.state.history_task = asyncio.create_task(history_refresh_loop(app.state))
        app.state.news_task = asyncio.create_task(news_poll_loop(app.state))
        app.state.econ_calendar_task = asyncio.create_task(econ_calendar_loop(app.state))

    try:
        yield
    finally:
        if app.state.poll_task is not None:
            await stop_task(app.state.poll_task)
        if app.state.history_task is not None:
            await stop_task(app.state.history_task)
        if app.state.news_task is not None:
            await stop_task(app.state.news_task)
        if app.state.econ_calendar_task is not None:
            await stop_task(app.state.econ_calendar_task)
        await asyncio.gather(
            *(provider.aclose() for provider in providers.values()),
            app.state.news_service.aclose(),
            app.state.econ_calendar_service.aclose(),
            return_exceptions=True,
        )


SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'self'; "
        b"script-src 'self' 'sha256-lzStUcqAQVQGXafGBmFjwHSxC/uBQ+JRbPX12Zt3sew='; "
        b"style-src 'self'; style-src-attr 'unsafe-inline'; "
        b"font-src 'self'; img-src 'self' data:; "
        b"connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; "
        b"frame-ancestors 'none'; form-action 'self'",
    ),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=()"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeadersMiddleware:
    """Attach browser hardening without BaseHTTPMiddleware's request buffering."""

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                if scope.get("scheme") == "https":
                    headers.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)  # type: ignore[operator]


app = FastAPI(title="Cross-Asset Board", lifespan=lifespan)
# Vercel's edge gzips responses; this covers local/VPS deployments too.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityHeadersMiddleware)


class CachedStaticFiles(StaticFiles):
    """Static files with immutable caching.

    Every static reference carries a ?v= cache-buster, so files can be
    cached for a year; version bumps change the URL.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")


def ensure_runtime_watchlist(settings: Settings) -> None:
    if settings.watchlist_path.exists():
        return
    if not settings.watchlist_seed_path.exists():
        return
    settings.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(settings.watchlist_seed_path, settings.watchlist_path)


def ensure_runtime_database(settings: Settings) -> None:
    if settings.database_path.exists():
        return
    if not settings.database_seed_path.exists():
        return
    if settings.database_path.resolve() == settings.database_seed_path.resolve():
        return
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(settings.database_seed_path, settings.database_path)


@app.get("/")
def index() -> FileResponse:
    # The HTML must always revalidate: it carries the ?v= cache-busters, so a
    # stale cached copy pins old immutable static assets indefinitely.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    # Browsers and link unfurlers request /favicon.ico unconditionally.
    return FileResponse(
        STATIC_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/health")
def health() -> dict[str, object]:
    payload: dict[str, object] = {"status": "ok"}
    service = getattr(app.state, "daily_board_service", None)
    if isinstance(service, DailyBoardService):
        payload["snapshots"] = service.snapshot_status()
    return payload


@app.get("/api/groups")
def groups() -> dict[str, object]:
    return groups_payload(app.state.groups)


def require_edit_token(
    x_edit_token: str | None = Header(default=None, alias="X-Edit-Token"),
) -> None:
    """Gate watchlist mutations when EDIT_TOKEN is configured.

    Read endpoints stay public so the board can be shared; without a token
    anyone with the URL could edit or wipe the persisted watchlists. An
    empty EDIT_TOKEN keeps the open behavior for local development.
    """
    token = app.state.settings.edit_token
    if not token:
        return
    # Compare as bytes: compare_digest on str raises TypeError for non-ASCII
    # (headers decode as latin-1), turning a garbage header into a 500.
    if not x_edit_token or not secrets.compare_digest(x_edit_token.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="edit_token_required")


@app.post("/api/groups", dependencies=[Depends(require_edit_token)])
async def create_group(request: GroupRequest) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = load_watchlists(app.state.settings.watchlist_path)
        name = clean_text(request.name)
        if name.upper() == MACRO_TAPE_GROUP_NAME:
            # Reserved: the virtual macro group is appended at fetch time;
            # a user group with the same name would be zipped against the
            # macro quotes (VIX/DXY prices on user assets).
            raise HTTPException(status_code=422, detail="group_name_reserved")
        if find_group(groups_current, name):
            raise HTTPException(status_code=409, detail="group_already_exists")
        groups_current.append(GroupConfig(name=name.upper(), assets=[]))
        save_watchlists(app.state.settings.watchlist_path, groups_current)
        app.state.groups = load_watchlists(app.state.settings.watchlist_path)
    return groups_payload(app.state.groups)


@app.delete("/api/groups/{group_name}", dependencies=[Depends(require_edit_token)])
async def delete_group(group_name: str) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = load_watchlists(app.state.settings.watchlist_path)
        group = find_group(groups_current, group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")
        groups_current = [item for item in groups_current if item is not group]
        save_watchlists(app.state.settings.watchlist_path, groups_current)
        app.state.groups = load_watchlists(app.state.settings.watchlist_path)
    return groups_payload(app.state.groups)


@app.post("/api/groups/{group_name}/assets", dependencies=[Depends(require_edit_token)])
async def create_asset(group_name: str, request: AssetRequest) -> dict[str, object]:
    symbol = clean_symbol(request.symbol)
    asset = AssetConfig(
        symbol=symbol,
        type=request.type,
        source=request.source,
        exchange=clean_optional(request.exchange),
        name=clean_optional(request.name),
    )
    await validate_symbol_exists(asset)
    async with app.state.watchlist_lock:
        groups_current = load_watchlists(app.state.settings.watchlist_path)
        group = find_group(groups_current, group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")

        if any(existing.symbol == symbol for existing in group.assets):
            raise HTTPException(status_code=409, detail="asset_already_exists")
        groups_current = [
            GroupConfig(
                name=item.name,
                assets=[*item.assets, asset] if item is group else item.assets,
            )
            for item in groups_current
        ]
        try:
            validate_watchlist_identities(groups_current)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="symbol_configuration_conflict",
            ) from exc
        save_watchlists(app.state.settings.watchlist_path, groups_current)
        app.state.groups = load_watchlists(app.state.settings.watchlist_path)
    return groups_payload(app.state.groups)


async def validate_symbol_exists(asset: AssetConfig) -> None:
    """Reject only a definitive provider not-found; outages must not block edits."""
    provider = app.state.quote_service.providers.get(asset.source)
    if provider is None:
        return
    try:
        status = await provider.validate_asset(asset)
    except Exception:
        status = "unavailable"
    if status == "not_found":
        raise HTTPException(status_code=422, detail="symbol_not_found")
    if status == "unavailable":
        logger.warning(
            "symbol %s added without provider verification (%s unavailable)",
            asset.symbol,
            asset.source,
        )


@app.delete("/api/groups/{group_name}/assets/{symbol}", dependencies=[Depends(require_edit_token)])
async def delete_asset(group_name: str, symbol: str) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = load_watchlists(app.state.settings.watchlist_path)
        group = find_group(groups_current, group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")
        wanted = clean_symbol(symbol)
        if not any(asset.symbol == wanted for asset in group.assets):
            raise HTTPException(status_code=404, detail="asset_not_found")
        groups_current = [
            GroupConfig(
                name=item.name,
                assets=[asset for asset in item.assets if asset.symbol != wanted]
                if item is group
                else item.assets,
            )
            for item in groups_current
        ]
        save_watchlists(app.state.settings.watchlist_path, groups_current)
        app.state.groups = load_watchlists(app.state.settings.watchlist_path)
    return groups_payload(app.state.groups)


def groups_payload(groups: list[GroupConfig]) -> dict[str, object]:
    return {
        "groups": [
            {
                "name": group.name,
                "assets": [
                    {
                        "symbol": asset.symbol,
                        "type": asset.type,
                        "source": asset.source,
                        "exchange": asset.exchange,
                        "name": asset.name,
                    }
                    for asset in group.assets
                ],
            }
            for group in groups
        ]
    }


def clean_text(value: str) -> str:
    return " ".join(value.strip().split())


def clean_symbol(value: str) -> str:
    return clean_text(value).upper()


def clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = clean_text(value)
    return cleaned or None


@app.get("/api/quotes")
async def quotes() -> dict[str, object]:
    # One snapshot: an edit completing across the heal await must not
    # rebuild the payload from swapped groups zipped against old quotes.
    groups = app.state.groups
    grouped = await app.state.quote_service.get_board_quotes(with_macro_group(groups))
    await _heal_stale_history()
    return await board_payload_async(app.state, groups, grouped)


@app.get("/api/news")
async def news() -> dict[str, object]:
    """Merged Telegram channel feed; also pushed over the WS as it updates."""
    service: NewsService = app.state.news_service
    return await service.get_feed()


async def _heal_stale_history() -> None:
    """Refresh a small batch of stale daily bars before building the board.

    Bounded by a hard timeout so a slow provider can never stall the quotes
    response by more than a few seconds; without a background scheduler
    (serverless) this is what keeps daily-board metrics from going stale.
    """
    try:
        await asyncio.wait_for(
            app.state.history_service.refresh_stale_daily_bars(app.state.groups),
            timeout=8.0,
        )
    except Exception:
        # Heal failures were previously silent: writes vanished with zero
        # diagnostics. Log and serve the cached bars.
        logger.exception("stale daily-bar heal failed")


@app.get("/api/crypto-etf-flows")
async def crypto_etf_flows() -> dict[str, object]:
    service: CryptoEtfFlowService = app.state.crypto_etf_flow_service
    return await service.get_flows()


@app.post("/api/reports", dependencies=[Depends(require_edit_token)])
async def create_report(request: ReportRequest) -> dict[str, object]:
    """Ingest one agent-written markdown report (e.g. a Hermes cron job).

    Only the newest report per slug is kept: same-day re-runs replace
    that day's report, and a new day's brief replaces the previous one.
    Any "Economic Calendar"/"Key Dates" section in the body feeds the
    key-dates panel; its rows mirror the report, so a re-run without
    the section clears them. A section whose heading mentions "fringe"
    feeds the Fringe Corner ideas ledger — an accruing book, NOT a
    mirror: only a same-day re-run can retract that day's new ideas.
    """
    report_date = request.date or datetime.now(UTC).date().isoformat()
    slug = _report_slug(request.slug or request.title)
    if not slug:
        raise HTTPException(status_code=422, detail="report_slug_invalid")
    events = parse_key_dates(request.body, default_date=report_date)
    # None: no fringe section — the ledger stays untouched.
    actions = parse_fringe_actions(request.body)

    def _ingest() -> int:
        return db.ingest_report(
            app.state.settings.database_path,
            slug=slug,
            report_date=report_date,
            title=clean_text(request.title),
            body=request.body,
            events=[(e.date, e.time, e.title, e.category) for e in events],
            fringe_actions=(
                [
                    (a.action, a.ticker, a.direction, a.text, a.horizon, a.target)
                    for a in actions
                ]
                if actions is not None
                else None
            ),
        )

    report_id = await asyncio.to_thread(_ingest)
    if actions:
        # Entry/exit stamping is best-effort at ingest; a provider outage
        # leaves prices null and /api/fringe re-stamps lazily. getattr:
        # unit tests exercise this route without running the lifespan.
        service = getattr(app.state, "fringe_service", None)
        if service is not None:
            try:
                await service.stamp_prices()
            except Exception:
                logger.warning("fringe price stamping failed", exc_info=True)
    return {
        "id": report_id,
        "slug": slug,
        "date": report_date,
        "key_dates": len(events),
        "fringe_actions": len(actions or []),
    }


@app.get("/api/reports")
async def reports(limit: int = Query(default=30, ge=1, le=200)) -> dict[str, object]:
    items = await asyncio.to_thread(db.load_reports, app.state.settings.database_path, limit)
    return {"reports": items}


@app.get("/api/reports/{report_id}")
async def report(report_id: int) -> dict[str, object]:
    item = await asyncio.to_thread(db.load_report, app.state.settings.database_path, report_id)
    if item is None:
        raise HTTPException(status_code=404, detail="report_not_found")
    return item


@app.delete("/api/reports/{report_id}", dependencies=[Depends(require_edit_token)])
async def delete_report(report_id: int) -> dict[str, object]:
    removed = await asyncio.to_thread(db.delete_report, app.state.settings.database_path, report_id)
    if not removed:
        raise HTTPException(status_code=404, detail="report_not_found")
    return {"status": "deleted"}


@app.get("/api/key-dates")
async def key_dates(
    days: int = Query(default=90, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    """Upcoming agent-fed calendar events, soonest first, with release data.

    "Today" is the US Eastern trading date — the panel renders an ET clock,
    and an evening UTC rollover must not drop the current session's events.
    Each item carries a `release` enrichment (null when unmatched); a
    calendar outage serves the plain payload, never an error.
    """
    # getattr: unit tests exercise this route without running the lifespan.
    service = getattr(app.state, "econ_calendar_service", None)
    return await key_dates_payload(
        app.state.settings.database_path, service, days=days, limit=limit
    )


@app.get("/api/fringe")
async def fringe() -> dict[str, object]:
    """The Fringe Corner book: open ideas marked to market + recent closes.

    Missing entry prices (a provider outage at ingest) are lazily
    re-stamped here; mark-to-market quotes sit behind a ~60s cache.
    """
    service: FringeService = app.state.fringe_service
    return await service.payload()


@app.get("/api/market-context")
async def market_context(days: int = Query(default=30)) -> dict[str, object]:
    """Continuous market memory for external agents (e.g. Hermes).

    Snapshot history, watchlist movers, accrued ETF flows, the next week
    of key dates, and the fringe book with P&L. `days` is clamped to
    7..90 (the caller is a bot, not a form); a broken piece degrades to
    empty, never a 500.
    """
    # getattr: unit tests exercise this route without running the lifespan.
    return await market_context_payload(
        app.state.settings.database_path,
        groups=getattr(app.state, "groups", []),
        econ_service=getattr(app.state, "econ_calendar_service", None),
        fringe_service=getattr(app.state, "fringe_service", None),
        days=days,
    )


def _report_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:64]


@app.get("/api/lighter-status")
def lighter_status() -> dict[str, object]:
    """Lighter feed diagnostics: cache freshness and active 429 cooldowns.

    Serverless deployments share egress IPs with other tenants, so secondary
    feeds (funding, tokenlist) can starve while quotes keep flowing; this
    shows which feed is failing on the running instance.
    """
    lighter = app.state.providers.get("lighter")
    if not isinstance(lighter, LighterProvider):
        return {"status": "unavailable"}
    return {"status": "ok", **lighter.status()}


@app.get("/api/yahoo-status")
def yahoo_status() -> dict[str, object]:
    """Cached Yahoo transport diagnostics from inside the running host."""
    global _yahoo_status_cache
    now = monotonic()
    with _yahoo_status_lock:
        if (
            _yahoo_status_cache is not None
            and now - _yahoo_status_cache[0] < YAHOO_STATUS_CACHE_SECONDS
        ):
            return _yahoo_status_cache[1]
        from app.providers.yahoo import YAHOO_SPARK_URLS, _get_json

        result: dict[str, object] = {"curl": shutil.which("curl")}
        try:
            payload = _get_json(
                YAHOO_SPARK_URLS[0],
                {"symbols": "SPY", "interval": "1d", "range": "1d"},
            )
            healthy = isinstance(payload, dict) and payload.get("spark")
            result["spark"] = "ok" if healthy else "unexpected_payload"
        except Exception as exc:
            result["spark_error"] = str(exc)[:300] or type(exc).__name__
        _yahoo_status_cache = (monotonic(), result)
        return result


@app.get("/api/snapshots")
async def snapshots(days: int = Query(default=30, ge=1, le=365)) -> dict[str, object]:
    """Persisted daily-board history: regime, breadth, and theme scores by date."""
    rows = await asyncio.to_thread(db.load_board_snapshots, app.state.settings.database_path, days)
    return {"snapshots": rows}


@app.get("/api/history/{symbol}")
async def history(
    symbol: str,
    interval: Annotated[HistoryInterval, Query()] = "1d",
    range_: Annotated[HistoryRange, Query(alias="range")] = "1y",
) -> dict[str, object]:
    clean = clean_symbol(symbol)
    if not clean or len(clean) > 24 or "/" in clean or "\\" in clean:
        raise HTTPException(status_code=422, detail="symbol_invalid")
    bars = await app.state.history_service.get_history(
        app.state.groups,
        clean,
        interval=interval,
        range_=range_,
    )
    return {
        "symbol": clean,
        "interval": interval,
        "range": range_,
        "bars": bars_payload(bars),
    }


@app.get("/api/profile/{symbol}")
async def profile(symbol: str) -> dict[str, object]:
    asset = find_asset(app.state.groups, clean_symbol(symbol))
    if asset is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    service: AssetProfileService = app.state.asset_profile_service
    return await asyncio.to_thread(service.get_profile, asset)


@app.websocket("/ws/quotes")
async def quotes_ws(websocket: WebSocket) -> None:
    manager: ConnectionManager = app.state.connection_manager
    await manager.connect(websocket)
    try:
        groups = app.state.groups
        grouped = await app.state.quote_service.get_board_quotes(with_macro_group(groups))
        await websocket.send_json(
            {"type": "quotes", "data": await board_payload_async(app.state, groups, grouped)}
        )
        # Register only after the snapshot send: a concurrent broadcast
        # could otherwise interleave ahead of the initial frame.
        manager.register(websocket)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
