import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware

from .api.routes.health import router as health_router
from .api.routes.process import router as process_router
from .api.routes.rides import router as rides_router
from .rate_limiter import limiter
from .services.supabase_client import get_supabase_service
from .upload_api import router as upload_router

logger = logging.getLogger(__name__)


def _recover_stale_processing_rides() -> None:
    try:
        svc = get_supabase_service()
        rows = svc.select("rides_metadata", "id", status="processing")
        if not rows:
            return
        ids = [row["id"] for row in rows if row.get("id")]
        logger.warning("Recovering %d stale processing ride(s): %s", len(ids), ids)
        for ride_id in ids:
            svc.update(
                "rides_metadata",
                {"status": "failed", "error_log": "Server restarted while ride was being processed"},
                id=ride_id,
            )
    except Exception as e:
        logger.error("Failed to recover stale processing rides: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _recover_stale_processing_rides()
    yield


app = FastAPI(title="SIPAT Process API", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(429, _rate_limit_exceeded_handler)

_cors_origins_str = os.getenv("CORS_ORIGINS", "")
if _cors_origins_str:
    _cors_origins = [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
else:
    _cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SlowAPIMiddleware)

app.include_router(upload_router)
app.include_router(rides_router)
app.include_router(process_router)
app.include_router(health_router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
