import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware

from .api.routes.community_photo import router as community_photo_router
from .api.routes.health import router as health_router
from .api.routes.process import router as process_router
from .api.routes.rides import router as rides_router
from .api.routes.verified_potholes import router as verified_potholes_router
from .background_tasks import signal_shutdown, wait_for_tasks
from .middleware import RequestIDMiddleware, request_id_var
from .rate_limiter import limiter
from .services.supabase_client import get_supabase_service
from .upload_api import router as upload_router


class _RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


def _configure_logging() -> None:
    fmt = "%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s"
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    handler.addFilter(_RequestIDFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Suppress noisy library loggers
    for name in (
        "azure",
        "azure.core",
        "azure.core.pipeline",
        "azure.core.pipeline.policies",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.storage",
        "azure.storage.blob",
        "httpx",
        "httpcore",
        "urllib3",
        "postgrest",
        "supabase",
        "storage3",
        "httpx._transports",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


_configure_logging()
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
                {"status": "failed", "error_log": "Server restarted while ride was being processed", "progress_pct": 0, "progress_stage": "", "progress_message": ""},
                id=ride_id,
            )
    except Exception as e:
        logger.error("Failed to recover stale processing rides: %s", e)


def _run_migrations() -> None:
    import os
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    url = os.environ.get("SUPABASE_URL")
    if not key or not url:
        logger.warning("Supabase credentials not set, skipping migrations")
        return
    try:
        import pg8000
        import ssl
        ref = url.split(".")[0].split("://")[1]
        conn = pg8000.connect(
            host=f"db.{ref}.supabase.co", port=5432,
            database="postgres", user="postgres", password=key,
            ssl_context=ssl.create_default_context(),
        )
        cur = conn.cursor()
        cur.execute("ALTER TABLE community_photos ADD COLUMN IF NOT EXISTS caption TEXT;")
        cur.execute("ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS progress_pct INTEGER DEFAULT 0;")
        cur.execute("ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS progress_stage TEXT DEFAULT '';")
        cur.execute("ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS progress_message TEXT DEFAULT '';")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Migration: added caption column to community_photos, progress columns to rides_metadata")
    except Exception as e:
        logger.warning("Direct DB migration failed (non-fatal): %s", e)
        logger.warning("If progress bar doesn't work, run this SQL in Supabase SQL Editor:")
        logger.warning("ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS progress_pct INTEGER DEFAULT 0;")
        logger.warning("ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS progress_stage TEXT DEFAULT '';")
        logger.warning("ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS progress_message TEXT DEFAULT '';")

    # Verify progress columns via REST API
    try:
        svc = get_supabase_service()
        svc.client.table("rides_metadata").select("progress_pct").limit(1).execute()
        logger.info("✓ progress_pct column exists and is readable")
    except Exception as e:
        logger.error("✗ progress_pct column MISSING or not readable: %s", e)
        logger.error("RUN THE SQL ABOVE IN SUPABASE SQL EDITOR then restart")

    # Test that we can actually WRITE to progress_pct (not just read)
    try:
        svc = get_supabase_service()
        rows = svc.client.table("rides_metadata").select("id,progress_pct").limit(1).execute().data
        if rows:
            test_id = rows[0]["id"]
            old_val = rows[0].get("progress_pct", 0) or 0
            svc.client.table("rides_metadata").update({"progress_pct": old_val}).eq("id", test_id).execute()
            logger.info("✓ progress_pct UPDATE works (wrote %s back to ride %s)", old_val, test_id[:8])
        else:
            logger.info("⊘ No rides in DB, skipping UPDATE test")
    except Exception as e:
        logger.error("✗ progress_pct UPDATE FAILED — progress bar will NOT work: %s", e)
        logger.error("Check RLS policies on rides_metadata in Supabase Dashboard → Authentication → Policies")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _recover_stale_processing_rides()
    _run_migrations()
    logger.info("Server started, accepting requests")
    yield
    # Graceful shutdown
    signal_shutdown()
    await wait_for_tasks()


app = FastAPI(title="SIPAT Process API", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(429, _rate_limit_exceeded_handler)

_cors_origins_str = os.getenv("CORS_ORIGINS", "")
if _cors_origins_str:
    _cors_origins = [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
else:
    _cors_origins = ["http://localhost:3000"]
    logger.warning(
        "CORS_ORIGINS not set; using default %s. "
        "Set the CORS_ORIGINS env var for production.",
        _cors_origins,
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestIDMiddleware)

app.include_router(upload_router)
app.include_router(rides_router)
app.include_router(process_router)
app.include_router(health_router)
app.include_router(verified_potholes_router)
app.include_router(community_photo_router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
