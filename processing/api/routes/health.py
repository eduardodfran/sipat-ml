import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import Request

from ...rate_limiter import HEALTH_LIMIT, limiter
from ...services.supabase_client import get_supabase_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_start_time = time.monotonic()


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    uptime_seconds: float
    version: str


class DetailedHealthResponse(HealthResponse):
    supabase: str


@router.get("/health", response_model=HealthResponse)
@limiter.limit(HEALTH_LIMIT)
async def health_check(request: Request):
    uptime = time.monotonic() - _start_time
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=round(uptime, 2),
        version="1.0.0",
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
    )
