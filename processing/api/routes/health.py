import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from ...middleware import request_id_var
from ...rate_limiter import HEALTH_LIMIT, limiter
from ...services.blob_storage import get_blob_storage_service
from ...services.supabase_client import get_supabase_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_start_time = time.monotonic()


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    uptime_seconds: float
    version: str
    request_id: str


class ReadinessResponse(BaseModel):
    status: str
    timestamp: str
    checks: dict[str, str]


class CircuitBreakerResponse(BaseModel):
    timestamp: str
    circuits: dict[str, dict]


class DetailedHealthResponse(HealthResponse):
    supabase: str


@router.get("/health", response_model=HealthResponse)
@limiter.limit(HEALTH_LIMIT)
async def health_check(request: Request):
    uptime = time.monotonic() - _start_time
    return JSONResponse(
        content=HealthResponse(
            status="healthy",
            timestamp=datetime.now(timezone.utc).isoformat(),
            uptime_seconds=round(uptime, 2),
            version="1.0.0",
            request_id=request_id_var.get(),
        ).model_dump(),
        headers={"Cache-Control": "public, max-age=10"},
    )


@router.get("/health/detail", response_model=DetailedHealthResponse)
@limiter.limit(HEALTH_LIMIT)
async def detailed_health_check(request: Request):
    uptime = time.monotonic() - _start_time

    supabase_status = "unknown"
    try:
        svc = get_supabase_service()
        svc.select("rides_metadata", "id", limit=1)
        supabase_status = "connected"
    except Exception as e:
        supabase_status = f"error: {type(e).__name__}"
        logger.warning("Supabase health check failed: %s", e)

    status = "healthy" if supabase_status == "connected" else "degraded"

    return DetailedHealthResponse(
        status=status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=round(uptime, 2),
        version="1.0.0",
        supabase=supabase_status,
        request_id=request_id_var.get(),
    )


@router.get("/health/ready", response_model=ReadinessResponse)
@limiter.limit(HEALTH_LIMIT)
async def readiness_check(request: Request):
    checks: dict[str, str] = {}

    try:
        svc = get_supabase_service()
        svc.select("rides_metadata", "id", limit=1)
        checks["supabase"] = "ok"
    except Exception as e:
        checks["supabase"] = f"error: {type(e).__name__}"

    try:
        blob_svc = get_blob_storage_service()
        blob_svc.client.get_service_properties()
        checks["azure_blob"] = "ok"
    except Exception as e:
        checks["azure_blob"] = f"error: {type(e).__name__}"

    all_ok = all(v == "ok" for v in checks.values())

    if not all_ok:
        logger.warning("Readiness check failed: %s", checks)

    return JSONResponse(
        content=ReadinessResponse(
            status="ready" if all_ok else "not_ready",
            timestamp=datetime.now(timezone.utc).isoformat(),
            checks=checks,
        ).model_dump(),
        headers={"Cache-Control": "public, max-age=10"},
    )


@router.get("/health/circuit-breakers", response_model=CircuitBreakerResponse)
@limiter.limit(HEALTH_LIMIT)
async def circuit_breaker_status(request: Request):
    """Return circuit breaker status for all external services."""
    circuits = {}
    try:
        svc = get_supabase_service()
        circuits["supabase"] = svc.get_circuit_status()
    except Exception:
        circuits["supabase"] = {"state": "unknown", "error": "Failed to get status"}
    try:
        blob_svc = get_blob_storage_service()
        circuits["blob_storage"] = blob_svc.get_circuit_status()
    except Exception:
        circuits["blob_storage"] = {"state": "unknown", "error": "Failed to get status"}

    return JSONResponse(
        content=CircuitBreakerResponse(
            timestamp=datetime.now(timezone.utc).isoformat(),
            circuits=circuits,
        ).model_dump(),
        headers={"Cache-Control": "public, max-age=10"},
    )
