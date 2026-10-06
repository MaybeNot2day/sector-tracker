"""Read-only AI and official-rate endpoints."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request

from app.services.ai_capex import AICapexError, AICapexService
from app.services.ai_data import AIDataError, AIDataService
from app.services.gpu_compute import GPUComputeError, GPUComputeService
from app.services.sofr import SOFRError, SOFRService

router = APIRouter(prefix="/api")


@router.get("/ai/models")
async def ai_models(request: Request) -> dict[str, object]:
    try:
        service = cast(AIDataService, request.app.state.ai_data_service)
        return await service.get_models()
    except AIDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/ai/token-index")
async def ai_token_index(request: Request) -> dict[str, object]:
    try:
        service = cast(AIDataService, request.app.state.ai_data_service)
        return await service.get_token_index()
    except AIDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/ai/capex")
async def ai_capex(request: Request) -> dict[str, object]:
    try:
        service = cast(AICapexService, request.app.state.ai_capex_service)
        return await service.get_capex()
    except AICapexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/ai/hardware")
async def ai_hardware(request: Request) -> dict[str, object]:
    try:
        service = cast(GPUComputeService, request.app.state.gpu_compute_service)
        return await service.get_hardware()
    except GPUComputeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sofr")
async def sofr(request: Request) -> dict[str, object]:
    try:
        service = cast(SOFRService, request.app.state.sofr_service)
        return await service.get_payload()
    except SOFRError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
