from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, time
from typing import Annotated, Literal, cast

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
from starlette.background import BackgroundTask
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
from app.providers.hyperliquid import HyperliquidProvider
from app.providers.stooq import StooqProvider
from app.providers.yahoo import YahooProvider
from app.scheduler import (
    ConnectionManager,
    ai_data_warm_loop,
    board_payload_async,
    earnings_warm_loop,
    econ_calendar_loop,
    history_refresh_loop,
    hyperliquid_discovery_loop,
    news_poll_loop,
    quote_poll_loop,
    sofr_refresh_loop,
    stop_task,
)
from app.services.ai_capex import AICapexError, AICapexService
from app.services.ai_data import AIDataError, AIDataService
from app.services.asset_profile import AssetProfileService
from app.services.component_trends import (
    component_trends_payload,
    fetch_trend_image,
    normalize_pushed_payload,
    save_pushed_payload,
)
from app.services.component_trends import store_file as component_trends_store
from app.services.crypto_etf_flows import CryptoEtfFlowService
from app.services.daily_board import DailyBoardService
from app.services.earnings import EarningsCalendarService, week_start
from app.services.econ_calendar import EconCalendarService, key_dates_payload
from app.services.fringe import FringeService, parse_fringe_actions
from app.services.gpu_compute import GPUComputeError, GPUComputeService
from app.services.history import HistoryService, bars_payload, find_asset
from app.services.hyperliquid_discovery import HyperliquidDiscoveryService
from app.services.key_dates import parse_key_dates
from app.services.macro import MACRO_TAPE_GROUP_NAME, with_macro_group
from app.services.market_context import market_context_payload
from app.services.news import NewsService
from app.services.options import MarketDataOptionsService, OptionsDataError
from app.services.quotes import QuoteService, quote_payload
from app.services.sofr import SOFRError, SOFRService
from app.services.symbol_logos import LOGO_KINDS, fetch_symbol_logo
from app.services.trends import group_trends_payload

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"
YAHOO_STATUS_CACHE_SECONDS = 60.0
HistoryInterval = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk", "1mo"]
HistoryRange = Literal["1d", "1w", "1mo", "3mo", "6mo", "ytd", "1y", "5y", "10y"]
US_OPTIONS_EXCHANGES = {"AMEX", "ARCA", "BATS", "CBOE", "NASDAQ", "NYSE", "NYSEARCA", "US"}
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
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"not a real calendar date: {value}") from exc
        # Reports may be dated for the next session, but a farther-future typo
        # would become the slug's "newest" brief and freeze all projections.
        if parsed > datetime.now(UTC).date() + timedelta(days=1):
            raise ValueError(f"report date is too far in the future: {value}")
        return value


class FringeCloseRequest(BaseModel):
    reason: str = Field(default="auto-stop", min_length=1, max_length=300)


def _log_loop_crash(task: asyncio.Task[None]) -> None:
    # A crashed poll loop previously died silently; the board just froze.
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task %s crashed", task.get_name(), exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    ensure_runtime_watchlist(settings)
    ensure_runtime_database(settings)
    base_groups = load_watchlists(settings.watchlist_path)
    db.init_db(settings.database_path)
    hyperliquid_discovery_service = HyperliquidDiscoveryService(
        settings.database_path,
        group_limit=settings.hyperliquid_discovery_group_limit,
    )
    groups = hyperliquid_discovery_service.merge_groups(base_groups)

    providers: dict[ProviderName, QuoteProvider] = {
        "yahoo": YahooProvider(),
        "hyperliquid": HyperliquidProvider(),
        "stooq": StooqProvider(),
    }

    app.state.settings = settings
    app.state.base_groups = base_groups
    app.state.groups = groups
    app.state.providers = providers
    app.state.hyperliquid_discovery_service = hyperliquid_discovery_service
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
    app.state.options_service = MarketDataOptionsService(
        settings.marketdata_token,
        base_url=settings.marketdata_base_url,
        cache_seconds=settings.options_cache_seconds,
    )
    app.state.news_service = NewsService(
        settings.news_channels,
        cache_seconds=settings.news_poll_seconds,
    )
    app.state.econ_calendar_service = EconCalendarService(
        cache_seconds=settings.econ_calendar_cache_seconds,
        countries=settings.econ_calendar_countries,
    )
    app.state.earnings_service = EarningsCalendarService()
    app.state.ai_data_service = AIDataService(settings.database_path)
    app.state.ai_capex_service = AICapexService(settings.database_path)
    app.state.gpu_compute_service = GPUComputeService(
        settings.database_path,
        api_key=settings.computeprices_api_key,
    )
    app.state.sofr_service = SOFRService(settings.database_path)
    app.state.connection_manager = ConnectionManager()
    app.state.watchlist_lock = asyncio.Lock()
    app.state.trends_revision = 0
    app.state.poll_task = None
    app.state.history_task = None
    app.state.news_task = None
    app.state.econ_calendar_task = None
    app.state.earnings_task = None
    app.state.ai_data_task = None
    app.state.hyperliquid_discovery_task = None
    app.state.sofr_task = None
    if settings.enable_background_tasks:
        app.state.poll_task = asyncio.create_task(
            quote_poll_loop(app.state), name="quote_poll_loop"
        )
        app.state.history_task = asyncio.create_task(
            history_refresh_loop(app.state), name="history_refresh_loop"
        )
        app.state.news_task = asyncio.create_task(news_poll_loop(app.state), name="news_poll_loop")
        app.state.econ_calendar_task = asyncio.create_task(
            econ_calendar_loop(app.state), name="econ_calendar_loop"
        )
        app.state.earnings_task = asyncio.create_task(
            earnings_warm_loop(app.state), name="earnings_warm_loop"
        )
        app.state.ai_data_task = asyncio.create_task(
            ai_data_warm_loop(app.state), name="ai_data_warm_loop"
        )
        app.state.hyperliquid_discovery_task = asyncio.create_task(
            hyperliquid_discovery_loop(app.state),
            name="hyperliquid_discovery_loop",
        )
        app.state.sofr_task = asyncio.create_task(
            sofr_refresh_loop(app.state),
            name="sofr_refresh_loop",
        )
        for task in (
            app.state.poll_task,
            app.state.history_task,
            app.state.news_task,
            app.state.econ_calendar_task,
            app.state.earnings_task,
            app.state.ai_data_task,
            app.state.hyperliquid_discovery_task,
            app.state.sofr_task,
        ):
            task.add_done_callback(_log_loop_crash)

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
        if app.state.earnings_task is not None:
            await stop_task(app.state.earnings_task)
        if app.state.ai_data_task is not None:
            await stop_task(app.state.ai_data_task)
        if app.state.hyperliquid_discovery_task is not None:
            await stop_task(app.state.hyperliquid_discovery_task)
        if app.state.sofr_task is not None:
            await stop_task(app.state.sofr_task)
        await asyncio.gather(
            *(provider.aclose() for provider in providers.values()),
            app.state.news_service.aclose(),
            app.state.options_service.aclose(),
            app.state.econ_calendar_service.aclose(),
            app.state.earnings_service.aclose(),
            app.state.ai_data_service.aclose(),
            app.state.ai_capex_service.aclose(),
            app.state.gpu_compute_service.aclose(),
            app.state.sofr_service.aclose(),
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
# Compress dynamic responses on the VPS; static assets are already compact.
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
        # index.html carries the ?v= cache-busters; an immutable copy would
        # pin old asset versions forever. It is normally served by the root
        # route, but the mount must not hand out a poisoned copy either.
        if Path(full_path).name == "index.html":
            response.headers["Cache-Control"] = "no-cache"
        else:
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
    return groups_payload(getattr(app.state, "base_groups", app.state.groups))


def _set_base_groups(base_groups: list[GroupConfig]) -> None:
    """Install curated groups and re-append persisted read-only discovery groups."""
    app.state.base_groups = base_groups
    discovery = getattr(app.state, "hyperliquid_discovery_service", None)
    app.state.groups = (
        discovery.merge_groups(base_groups)
        if isinstance(discovery, HyperliquidDiscoveryService)
        else base_groups
    )
    app.state.trends_revision = int(getattr(app.state, "trends_revision", 0)) + 1
    _trends_cache.clear()


def require_edit_token(
    x_edit_token: str | None = Header(default=None, alias="X-Edit-Token"),
) -> None:
    """Gate every mutation and database export behind explicit configuration."""
    settings = app.state.settings
    token = settings.edit_token
    if not token:
        if getattr(settings, "allow_unsafe_edits", False):
            return
        raise HTTPException(status_code=503, detail="edit_token_not_configured")
    # Compare as bytes: compare_digest on str raises TypeError for non-ASCII
    # (headers decode as latin-1), turning a garbage header into a 500.
    if not x_edit_token or not secrets.compare_digest(x_edit_token.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="edit_token_required")


@app.post("/api/groups", dependencies=[Depends(require_edit_token)])
async def create_group(request: GroupRequest) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = await asyncio.to_thread(load_watchlists, app.state.settings.watchlist_path)
        name = clean_text(request.name)
        if name.upper() == MACRO_TAPE_GROUP_NAME:
            # Reserved: the virtual macro group is appended at fetch time;
            # a user group with the same name would be zipped against the
            # macro quotes (VIX/DXY prices on user assets).
            raise HTTPException(status_code=422, detail="group_name_reserved")
        if find_group(groups_current, name):
            raise HTTPException(status_code=409, detail="group_already_exists")
        groups_current.append(GroupConfig(name=name.upper(), assets=[]))
        await asyncio.to_thread(save_watchlists, app.state.settings.watchlist_path, groups_current)
        _set_base_groups(groups_current)
        # Trend bands are keyed by the watchlist shape; a cached payload would
        # keep serving the pre-edit groups for up to 5 minutes.
        _trends_cache.clear()
    return groups_payload(app.state.base_groups)


@app.delete("/api/groups/{group_name}", dependencies=[Depends(require_edit_token)])
async def delete_group(group_name: str) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = await asyncio.to_thread(load_watchlists, app.state.settings.watchlist_path)
        group = find_group(groups_current, group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")
        groups_current = [item for item in groups_current if item is not group]
        await asyncio.to_thread(save_watchlists, app.state.settings.watchlist_path, groups_current)
        _set_base_groups(groups_current)
        _trends_cache.clear()
    return groups_payload(app.state.base_groups)


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
        groups_current = await asyncio.to_thread(load_watchlists, app.state.settings.watchlist_path)
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
        await asyncio.to_thread(save_watchlists, app.state.settings.watchlist_path, groups_current)
        _set_base_groups(groups_current)
        _trends_cache.clear()
    return groups_payload(app.state.base_groups)


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
        groups_current = await asyncio.to_thread(load_watchlists, app.state.settings.watchlist_path)
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
        await asyncio.to_thread(save_watchlists, app.state.settings.watchlist_path, groups_current)
        _set_base_groups(groups_current)
        _trends_cache.clear()
    return groups_payload(app.state.base_groups)


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
    # One snapshot: an edit landing mid-request must not rebuild the payload
    # from swapped groups zipped against old quotes.
    groups = app.state.groups
    grouped = await app.state.quote_service.get_board_quotes(
        with_macro_group(groups),
        allow_stale=app.state.settings.enable_background_tasks,
    )
    _schedule_history_heal()
    return await board_payload_async(app.state, groups, grouped)


async def _resolve_lookup_asset(symbol: str) -> AssetConfig:
    """Best-effort asset identity for a free-typed watch-list symbol.

    Board config wins (it carries curated type/source/exchange); otherwise the
    Hyperliquid market map claims crypto perps and xyz TradFi synthetics; any
    remaining ticker defaults to a Yahoo equity, which also covers ETFs,
    futures ("GC=F") and FX the way Yahoo spells them.
    """
    asset = find_asset(app.state.groups, symbol)
    if asset is not None:
        return asset
    hyperliquid = app.state.providers.get("hyperliquid")
    if isinstance(hyperliquid, HyperliquidProvider):
        try:
            if await hyperliquid.has_market(symbol):
                if hyperliquid.is_crypto_market(symbol):
                    return AssetConfig(symbol=symbol, type="crypto_perp", source="hyperliquid")
                if hyperliquid.is_tradfi_market(symbol):
                    return AssetConfig(symbol=symbol, type="equity", source="hyperliquid")
        except Exception:
            logger.warning("hyperliquid lookup resolution failed for %s", symbol, exc_info=True)
    return AssetConfig(symbol=symbol, type="equity", source="yahoo")


@app.get("/api/quotes/lookup")
async def quotes_lookup(
    symbols: str = Query(min_length=1, max_length=1200),
) -> dict[str, object]:
    """Quotes for arbitrary symbols (personal named watch lists).

    Unlike /api/quotes this is not tied to the board groups: each symbol is
    resolved independently, so a list can mix crypto perps, equities, ETFs
    and futures. Symbols that fail to quote are simply absent from the map.
    """
    requested: list[str] = []
    for raw in symbols.split(","):
        clean = clean_symbol(raw)
        if not clean or len(clean) > 24 or "/" in clean or "\\" in clean:
            continue
        if clean not in requested:
            requested.append(clean)
    if not requested:
        raise HTTPException(status_code=422, detail="symbols_invalid")
    requested = requested[:60]
    assets = [await _resolve_lookup_asset(symbol) for symbol in requested]
    quotes_by_symbol = await app.state.quote_service.get_lookup_quotes(assets)
    return {
        "quotes": {
            symbol: quote_payload(quotes_by_symbol[symbol])
            for symbol in requested
            if symbol in quotes_by_symbol
        },
        "assets": {
            asset.symbol.upper(): {
                "type": asset.type,
                "source": asset.source,
                "name": asset.name,
            }
            for asset in assets
        },
    }


@app.get("/api/news")
async def news() -> dict[str, object]:
    """Merged Telegram channel feed; also pushed over the WS as it updates."""
    service: NewsService = app.state.news_service
    return await service.get_feed()


@app.get("/api/news/map")
async def news_map() -> dict[str, object]:
    """Semantic treemap over the currently cached news feed."""
    service: NewsService = app.state.news_service
    return service.map_payload()


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


# Fire-and-forget with a floor: /api/quotes must never wait on the heal, and
# weekend polls (when nothing can refresh) must not respawn it every request.
_HEAL_MIN_INTERVAL_SECONDS = 60.0
_heal_task: asyncio.Task[None] | None = None
# None (not 0.0): monotonic() is near-zero right after host boot, which would
# otherwise read as a live interval window and skip the first heal.
_heal_started: float | None = None


def _schedule_history_heal() -> None:
    """Start a background heal unless one is running or ran within the floor."""
    global _heal_task, _heal_started
    if _heal_task is not None and not _heal_task.done():
        return
    now = monotonic()
    if _heal_started is not None and now - _heal_started < _HEAL_MIN_INTERVAL_SECONDS:
        return
    _heal_started = now
    # _heal_stale_history catches everything itself, so no done-callback.
    _heal_task = asyncio.create_task(_heal_stale_history())


@app.get("/api/crypto-etf-flows")
async def crypto_etf_flows() -> dict[str, object]:
    service: CryptoEtfFlowService = app.state.crypto_etf_flow_service
    return await service.get_flows()


@app.post("/api/reports", dependencies=[Depends(require_edit_token)])
async def create_report(request: ReportRequest) -> dict[str, object]:
    """Ingest one agent-written markdown report (e.g. a Hermes cron job).

    Reports are archived by (slug, date); same-day re-runs replace that day's
    row while prior days remain readable in the report library.
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
    # Parsers walk the whole markdown body; keep the event loop free.
    events = await asyncio.to_thread(parse_key_dates, request.body, default_date=report_date)
    # None: no fringe section — the ledger stays untouched.
    actions = await asyncio.to_thread(parse_fringe_actions, request.body)

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
                    (
                        a.action,
                        a.ticker,
                        a.direction,
                        a.text,
                        a.horizon,
                        a.target,
                        a.confidence,
                        a.stop,
                    )
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
async def reports(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    slug: str | None = Query(default=None, min_length=1, max_length=64),
    before_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    before_created_at: str | None = Query(default=None, max_length=64),
    before_id: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    cursor_values = (before_date, before_created_at, before_id)
    if any(value is not None for value in cursor_values) and not all(
        value is not None for value in cursor_values
    ):
        raise HTTPException(status_code=422, detail="report_cursor_incomplete")
    if offset and before_date is not None:
        raise HTTPException(status_code=422, detail="report_cursor_with_offset")
    before = (
        (before_date, before_created_at, before_id)
        if before_date is not None and before_created_at is not None and before_id is not None
        else None
    )
    return await asyncio.to_thread(
        db.load_reports,
        app.state.settings.database_path,
        limit,
        offset=offset,
        slug=slug,
        before=before,
    )


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


@app.get("/api/earnings")
async def earnings_calendar(start: str | None = Query(default=None)) -> dict[str, object]:
    """The weekly earnings calendar: Mon-Fri reporting companies from Nasdaq.

    Defaults to the current trading week (next week from a weekend); `start`
    accepts any date and snaps back to its Monday. Board-held symbols rank
    first per day and carry an options-implied move when available.
    """
    if start is None:
        monday = week_start(datetime.now(UTC).date())
    else:
        try:
            monday = week_start(date.fromisoformat(start))
        except ValueError:
            raise HTTPException(status_code=422, detail="start_invalid") from None
        # week_start pushes weekend dates forward; explicit navigation should
        # land on the week containing the requested date instead.
        if monday > date.fromisoformat(start):
            monday -= timedelta(days=7)
    service: EarningsCalendarService = app.state.earnings_service
    held = {
        asset.symbol.upper() for group in getattr(app.state, "groups", []) for asset in group.assets
    }
    return await service.get_week_cached(monday, held, getattr(app.state, "options_service", None))


@app.get("/api/fringe")
async def fringe() -> dict[str, object]:
    """The Fringe Corner book: open ideas marked to market + recent closes.

    Missing entry prices (a provider outage at ingest) are lazily
    re-stamped here; mark-to-market quotes sit behind a ~60s cache.
    """
    service: FringeService = app.state.fringe_service
    return await service.payload()


@app.post("/api/fringe/{idea_id}/close", dependencies=[Depends(require_edit_token)])
async def close_fringe_position(idea_id: int, request: FringeCloseRequest) -> dict[str, object]:
    """Close one open idea at the current mark — the intraday auto-stop path.

    The exit price is always the board's own fresh mark, never caller input;
    honest slippage on gaps is the point.
    """
    service: FringeService = app.state.fringe_service
    try:
        item = await service.close_at_market(idea_id, request.reason)
    except LookupError:
        raise HTTPException(status_code=404, detail="idea_not_open") from None
    except RuntimeError:
        raise HTTPException(status_code=503, detail="mark_unavailable") from None
    return {"closed": item}


# A crashed request or killed download can orphan the FileResponse temp file;
# sweep old ones on the next backup call instead of leaking /tmp forever.
_BACKUP_TEMP_MAX_AGE_SECONDS = 3600.0


def _remove_stale_backup_temps() -> None:
    cutoff = time() - _BACKUP_TEMP_MAX_AGE_SECONDS
    for leftover in Path(tempfile.gettempdir()).glob("board-backup-*.sqlite3"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink(missing_ok=True)
        except OSError:
            continue


@app.get("/api/backup", dependencies=[Depends(require_edit_token)])
async def database_backup() -> FileResponse:
    """Stream a consistent SQLite snapshot for off-box backups.

    The Fringe ledger and equity history are irreplaceable accumulated
    state; the nightly hermes-box job pulls this into the Syncthing-mirrored
    vault so the track record survives the droplet.
    """
    await asyncio.to_thread(_remove_stale_backup_temps)
    handle, temp_path = tempfile.mkstemp(prefix="board-backup-", suffix=".sqlite3")
    os.close(handle)
    os.unlink(temp_path)  # VACUUM INTO refuses an existing target
    try:
        await asyncio.to_thread(
            db.snapshot_database, app.state.settings.database_path, Path(temp_path)
        )
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return FileResponse(
        temp_path,
        media_type="application/octet-stream",
        filename="market_board.sqlite3",
        background=BackgroundTask(os.unlink, temp_path),
    )


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


@app.get("/api/hyperliquid-status", dependencies=[Depends(require_edit_token)])
def hyperliquid_status() -> dict[str, object]:
    """Hyperliquid feed and automatic-listing discovery diagnostics."""
    hyperliquid = app.state.providers.get("hyperliquid")
    if not isinstance(hyperliquid, HyperliquidProvider):
        return {"status": "unavailable"}
    payload: dict[str, object] = {"status": "ok", **hyperliquid.status()}
    discovery = getattr(app.state, "hyperliquid_discovery_service", None)
    if isinstance(discovery, HyperliquidDiscoveryService):
        payload["discovery"] = discovery.status()
    return payload


@app.get("/api/yahoo-status", dependencies=[Depends(require_edit_token)])
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


@app.get("/api/ai/models")
async def ai_models() -> dict[str, object]:
    """Current text-model catalog and normalized token prices."""
    try:
        service = cast(AIDataService, app.state.ai_data_service)
        return await service.get_models()
    except AIDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/ai/token-index")
async def ai_token_index() -> dict[str, object]:
    """Usage-weighted token-price proxy built from public OpenRouter data."""
    try:
        service = cast(AIDataService, app.state.ai_data_service)
        return await service.get_token_index()
    except AIDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/ai/capex")
async def ai_capex() -> dict[str, object]:
    """Reported hyperscaler capex used as an AI infrastructure proxy."""
    try:
        service = cast(AICapexService, app.state.ai_capex_service)
        return await service.get_capex()
    except AICapexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/ai/hardware")
async def ai_hardware() -> dict[str, object]:
    """Current normalized cloud GPU rental prices and provider offers."""
    try:
        service = cast(GPUComputeService, app.state.gpu_compute_service)
        return await service.get_hardware()
    except GPUComputeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sofr")
async def sofr() -> dict[str, object]:
    """Official SOFR rate distribution and history from the New York Fed."""
    try:
        service = cast(SOFRService, app.state.sofr_service)
        return await service.get_payload()
    except SOFRError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# Trend bands aggregate every cached daily bar on each call; a short TTL
# keeps tab switches and range flips from re-scanning SQLite.
_trends_cache: dict[int, tuple[float, int, dict[str, object]]] = {}
_TRENDS_CACHE_SECONDS = 300.0
_TRENDS_CACHE_MAX = 8


@app.get("/api/trends")
async def trends(days: int = Query(default=90, ge=14, le=365)) -> dict[str, object]:
    """Per-group normalized performance bands (min/avg/max, indexed to 100)."""
    cached = _trends_cache.get(days)
    revision = int(getattr(app.state, "trends_revision", 0))
    if (
        cached is not None
        and cached[1] == revision
        and monotonic() - cached[0] < _TRENDS_CACHE_SECONDS
    ):
        return cached[2]
    payload = await asyncio.to_thread(
        group_trends_payload, app.state.settings.database_path, app.state.groups, days
    )
    _trends_cache[days] = (monotonic(), revision, payload)
    # days spans 14-365; without a cap a scanner could hold 350 payloads.
    if len(_trends_cache) > _TRENDS_CACHE_MAX:
        del _trends_cache[min(_trends_cache, key=lambda key: _trends_cache[key][0])]
    return payload


@app.get("/api/component-trends")
async def component_trends() -> dict[str, object]:
    """PCPartPicker daily component price-trend charts, cached server-side."""
    return await component_trends_payload(component_trends_store(app.state.settings.database_path))


@app.post("/api/component-trends", dependencies=[Depends(require_edit_token)])
async def push_component_trends(payload: dict[str, object]) -> dict[str, object]:
    """Ingest a scraped component-trends payload from an off-box pusher.

    Cloudflare 403s PCPartPicker page fetches from datacenter IP ranges, so
    a residential-network machine runs scripts/component_trends_pusher.py
    and POSTs the gallery lists here (image URLs stay CDN-prefix locked).
    """
    normalized = normalize_pushed_payload(payload)
    if normalized is None:
        raise HTTPException(status_code=422, detail="component_trends_invalid")
    await asyncio.to_thread(
        save_pushed_payload,
        component_trends_store(app.state.settings.database_path),
        normalized,
    )
    categories = cast("list[dict[str, object]]", normalized["categories"])
    return {"status": "ok", "categories": len(categories)}


@app.get("/api/component-image")
async def component_image(src: str = Query(min_length=1, max_length=300)) -> Response:
    """Same-origin proxy for PCPartPicker trend PNGs (CDN-prefix locked)."""
    result = await fetch_trend_image(src)
    if result is None:
        raise HTTPException(status_code=404, detail="image_not_found")
    data, content_type = result
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=21600"},
    )


_LOGO_SYMBOL = re.compile(r"[A-Z0-9.\-]{1,24}")


@app.get("/api/symbol-logo/{symbol}")
async def symbol_logo(symbol: str, kind: str = Query(min_length=1, max_length=10)) -> Response:
    """Same-origin proxy for asset logos (Hyperliquid coins, Parqet tickers)."""
    clean = clean_symbol(symbol)
    if kind not in LOGO_KINDS or not _LOGO_SYMBOL.fullmatch(clean):
        raise HTTPException(status_code=422, detail="logo_request_invalid")
    result = await fetch_symbol_logo(clean, kind)
    if result is None:
        # Cacheable 404: the browser hides the img and won't re-ask all day.
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=21600"})
    data, content_type = result
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/options/{symbol}")
async def options_snapshot(
    symbol: str,
    expiration: str | None = Query(default=None),
) -> dict[str, object]:
    clean = clean_symbol(symbol)
    if not clean or len(clean) > 24 or "/" in clean or "\\" in clean:
        raise HTTPException(status_code=422, detail="symbol_invalid")
    asset = find_asset(app.state.groups, clean)
    if asset is None:
        fringe_service: FringeService = app.state.fringe_service
        asset = await fringe_service.resolve_known_asset(clean)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    exchange = str(asset.exchange or "").upper()
    if asset.type not in {"equity", "etf"} or (exchange and exchange not in US_OPTIONS_EXCHANGES):
        raise HTTPException(status_code=422, detail="options_asset_unsupported")
    service: MarketDataOptionsService = app.state.options_service
    try:
        return await service.get_snapshot(clean, expiration)
    except OptionsDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


@app.get("/api/history/{symbol}")
async def history(
    symbol: str,
    interval: Annotated[HistoryInterval, Query()] = "1d",
    range_: Annotated[HistoryRange, Query(alias="range")] = "1y",
) -> dict[str, object]:
    clean = clean_symbol(symbol)
    if not clean or len(clean) > 24 or "/" in clean or "\\" in clean:
        raise HTTPException(status_code=422, detail="symbol_invalid")
    fallback_asset = None
    if find_asset(app.state.groups, clean) is None:
        fringe_service: FringeService = app.state.fringe_service
        fallback_asset = await fringe_service.resolve_known_asset(clean)
    bars = await app.state.history_service.get_history(
        app.state.groups,
        clean,
        interval=interval,
        range_=range_,
        fallback_asset=fallback_asset,
    )
    return {
        "symbol": clean,
        "interval": interval,
        "range": range_,
        "bars": bars_payload(bars),
    }


@app.get("/api/profile/{symbol}")
async def profile(symbol: str) -> dict[str, object]:
    clean = clean_symbol(symbol)
    asset = find_asset(app.state.groups, clean)
    if asset is None:
        fringe_service: FringeService = app.state.fringe_service
        asset = await fringe_service.resolve_known_asset(clean)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    service: AssetProfileService = app.state.asset_profile_service
    return await service.get_profile(asset)


@app.websocket("/ws/quotes")
async def quotes_ws(websocket: WebSocket) -> None:
    manager: ConnectionManager = app.state.connection_manager
    await manager.connect(websocket)
    try:
        groups = app.state.groups
        grouped = await app.state.quote_service.get_board_quotes(
            with_macro_group(groups),
            allow_stale=app.state.settings.enable_background_tasks,
        )
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
