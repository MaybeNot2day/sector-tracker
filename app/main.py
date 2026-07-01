from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.config import Settings, find_group, load_watchlists, save_watchlists
from app.models import AssetConfig, AssetType, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.finnhub import FinnhubProvider
from app.providers.hyperliquid import HyperliquidProvider
from app.providers.stooq import StooqProvider
from app.providers.yahoo import YahooProvider
from app.scheduler import ConnectionManager, history_refresh_loop, quote_poll_loop, stop_task
from app.services.asset_profile import AssetProfileService
from app.services.crypto_etf_flows import CryptoEtfFlowService
from app.services.daily_board import DailyBoardService
from app.services.history import HistoryService, bars_payload, find_asset
from app.services.quotes import QuoteService, grouped_quotes_payload

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"


class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class AssetRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=24)
    type: AssetType = "equity"
    source: ProviderName = "yahoo"
    exchange: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=96)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    ensure_runtime_watchlist(settings)
    ensure_runtime_database(settings)
    groups = load_watchlists(settings.watchlist_path)
    db.init_db(settings.database_path)

    providers: dict[ProviderName, QuoteProvider] = {
        "yahoo": YahooProvider(),
        "hyperliquid": HyperliquidProvider(),
        "stooq": StooqProvider(),
    }
    if settings.finnhub_api_key:
        providers["finnhub"] = FinnhubProvider(settings.finnhub_api_key)

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
    )
    app.state.asset_profile_service = AssetProfileService()
    app.state.connection_manager = ConnectionManager()
    app.state.watchlist_lock = asyncio.Lock()
    app.state.poll_task = None
    app.state.history_task = None
    if settings.enable_background_tasks:
        app.state.poll_task = asyncio.create_task(quote_poll_loop(app.state))
        app.state.history_task = asyncio.create_task(history_refresh_loop(app.state))

    try:
        yield
    finally:
        if app.state.poll_task is not None:
            await stop_task(app.state.poll_task)
        if app.state.history_task is not None:
            await stop_task(app.state.history_task)


app = FastAPI(title="Cross-Asset Board", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/groups")
def groups() -> dict[str, object]:
    return groups_payload(app.state.groups)


@app.post("/api/groups")
async def create_group(request: GroupRequest) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = load_watchlists(app.state.settings.watchlist_path)
        name = clean_text(request.name)
        if find_group(groups_current, name):
            raise HTTPException(status_code=409, detail="group_already_exists")
        groups_current.append(GroupConfig(name=name.upper(), assets=[]))
        save_watchlists(app.state.settings.watchlist_path, groups_current)
        app.state.groups = load_watchlists(app.state.settings.watchlist_path)
    return groups_payload(app.state.groups)


@app.delete("/api/groups/{group_name}")
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


@app.post("/api/groups/{group_name}/assets")
async def create_asset(group_name: str, request: AssetRequest) -> dict[str, object]:
    async with app.state.watchlist_lock:
        groups_current = load_watchlists(app.state.settings.watchlist_path)
        group = find_group(groups_current, group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")

        symbol = clean_symbol(request.symbol)
        if any(asset.symbol == symbol for asset in group.assets):
            raise HTTPException(status_code=409, detail="asset_already_exists")
        asset = AssetConfig(
            symbol=symbol,
            type=request.type,
            source=request.source,
            exchange=clean_optional(request.exchange),
            name=clean_optional(request.name),
        )
        groups_current = [
            GroupConfig(
                name=item.name,
                assets=[*item.assets, asset] if item is group else item.assets,
            )
            for item in groups_current
        ]
        save_watchlists(app.state.settings.watchlist_path, groups_current)
        app.state.groups = load_watchlists(app.state.settings.watchlist_path)
    return groups_payload(app.state.groups)


@app.delete("/api/groups/{group_name}/assets/{symbol}")
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
    grouped = await app.state.quote_service.get_board_quotes(app.state.groups)
    return board_payload(grouped)


@app.get("/api/crypto-etf-flows")
async def crypto_etf_flows() -> dict[str, object]:
    return await app.state.crypto_etf_flow_service.get_flows()


@app.get("/api/history/{symbol}")
async def history(
    symbol: str,
    interval: str = Query(default="1d"),
    range_: str = Query(default="1y", alias="range"),
) -> dict[str, object]:
    bars = await app.state.history_service.get_history(
        app.state.groups,
        symbol,
        interval=interval,
        range_=range_,
    )
    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "range": range_,
        "bars": bars_payload(bars),
    }


@app.get("/api/profile/{symbol}")
async def profile(symbol: str) -> dict[str, object]:
    asset = find_asset(app.state.groups, clean_symbol(symbol))
    if asset is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    return await asyncio.to_thread(app.state.asset_profile_service.get_profile, asset)


@app.websocket("/ws/quotes")
async def quotes_ws(websocket: WebSocket) -> None:
    manager: ConnectionManager = app.state.connection_manager
    await manager.connect(websocket)
    try:
        grouped = await app.state.quote_service.get_board_quotes(app.state.groups)
        await websocket.send_json({"type": "quotes", "data": board_payload(grouped)})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


def board_payload(grouped: dict[str, list[Quote]]) -> dict[str, object]:
    summaries = app.state.daily_board_service.market_summaries(app.state.groups, grouped)
    payload = grouped_quotes_payload(app.state.groups, grouped, summaries=summaries)
    payload["overview"] = app.state.daily_board_service.build(app.state.groups, grouped)
    return payload
