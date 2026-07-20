import logging
import traceback

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from ...background_tasks import submit_background_task
from ...rate_limiter import PROCESS_LIMIT, limiter
from ...services.supabase_client import get_supabase_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["process"])


class ProcessResponse(BaseModel):
    status: str
    ride_id: str
    message: str


async def _run_process_async(ride_id: str) -> None:
    print(f"[DIAG] _run_process_async started for {ride_id[:8]}", flush=True)
    logger.info("[%s] Background task started", ride_id[:8])
    from ...pipeline.worker import RideProcessor

    try:
        logger.info("[%s] Fetching ride data from DB", ride_id[:8])
        svc = get_supabase_service()
        rows = svc.select("rides_metadata", "*", id=ride_id)
        if not rows:
            print(f"[DIAG] _run_process_async: ride {ride_id[:8]} not found in DB", flush=True)
            logger.warning("Ride %s not found", ride_id)
            return
        ride = rows[0]
        ride["status"] = "processing"
        try:
            processor = RideProcessor()
            result = await processor.process_ride_async(ride)
            logger.info("Completed ride %s: %d detections", ride_id, result["raw_detection_count"])
            print(f"[DIAG] _run_process_async: ride {ride_id[:8]} COMPLETED successfully", flush=True)
        except Exception as exc:
            error_message = str(exc)
            traceback_text = traceback.format_exc()
            print(f"[DIAG] _run_process_async: ride {ride_id[:8]} FAILED: {exc}", flush=True)
            try:
                svc.update("rides_metadata", {"status": "failed", "error_log": error_message, "progress_pct": 0, "progress_stage": "failed", "progress_message": f"Error: {error_message}"}, id=ride_id)
                print(f"[DIAG] _run_process_async: ride {ride_id[:8]} marked as failed in DB", flush=True)
            except Exception as mark_exc:
                print(f"[DIAG] _run_process_async: CRITICAL - failed to mark ride {ride_id[:8]} as failed: {mark_exc}", flush=True)
                logger.error("Failed to mark ride %s as failed: %s", ride_id, mark_exc)
            logger.error("Background processing failed for ride %s: %s", ride_id, exc)
            logger.debug(traceback_text)
    except Exception as e:
        print(f"[DIAG] _run_process_async: OUTER exception for ride {ride_id[:8]}: {e}", flush=True)
        logger.error("Setup error in background processing for ride %s: %s", ride_id, e)
        try:
            svc2 = get_supabase_service()
            svc2.update("rides_metadata", {"status": "failed", "error_log": str(e), "progress_pct": 0, "progress_stage": "failed", "progress_message": f"Setup error: {e}"}, id=ride_id)
            print(f"[DIAG] _run_process_async: ride {ride_id[:8]} marked as failed (outer catch)", flush=True)
        except Exception as mark_exc:
            print(f"[DIAG] _run_process_async: CRITICAL - could not mark ride {ride_id[:8]} as failed: {mark_exc}", flush=True)
            logger.error("Failed to mark ride %s as failed from outer handler: %s", ride_id, mark_exc)


@router.post("/process/{ride_id}", response_model=ProcessResponse)
@limiter.limit(PROCESS_LIMIT)
async def process_ride(
    request: Request,
    ride_id: str,
    authorization: str = Header(None),
):
    print(f"[DIAG] process_ride called for {ride_id[:8]}", flush=True)
    svc = get_supabase_service()

    auth = svc.validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    rows = svc.select("rides_metadata", "id,user_id,status", id=ride_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Ride {ride_id} not found")

    ride = rows[0]
    if ride.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You can only process your own rides")

    current_status = ride.get("status", "")
    if current_status == "processing":
        raise HTTPException(status_code=409, detail="Ride is already being processed")
    if current_status == "completed":
        raise HTTPException(status_code=409, detail="Ride has already been processed")

    result = svc.client.table("rides_metadata").update({
        "status": "processing",
        "progress_pct": 1,
        "progress_stage": "starting",
        "progress_message": "Processing request received...",
    }).eq("id", ride_id).eq("status", current_status).execute()
    if not result.data:
        raise HTTPException(status_code=409, detail="Ride status changed, please retry")

    task = await submit_background_task(_run_process_async(ride_id), name=f"process_{ride_id}")
    print(f"[DIAG] Background task submitted for {ride_id[:8]}", flush=True)
    logger.info("Background task '%s' submitted for ride %s (task_id=%s)", task.get_name(), ride_id[:8], id(task))

    return ProcessResponse(
        status="accepted",
        ride_id=ride_id,
        message="Processing started in background",
    )
