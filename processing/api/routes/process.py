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
    from ...pipeline.worker import RideProcessor

    try:
        svc = get_supabase_service()
        rows = svc.select("rides_metadata", "*", id=ride_id)
        if not rows:
            logger.warning("Ride %s not found", ride_id)
            return
        ride = rows[0]
        ride["status"] = "processing"
        try:
            processor = RideProcessor()
            result = await processor.process_ride_async(ride)
            logger.info("Completed ride %s: %d detections", ride_id, result["raw_detection_count"])
        except Exception as exc:
            error_message = str(exc)
            traceback_text = traceback.format_exc()
            try:
                svc.update("rides_metadata", {"status": "failed", "error_log": error_message}, id=ride_id)
            except Exception as mark_exc:
                logger.error("Failed to mark ride %s as failed: %s", ride_id, mark_exc)
            logger.error("Background processing failed for ride %s: %s", ride_id, exc)
            logger.debug(traceback_text)
    except Exception as e:
        logger.error("Setup error in background processing for ride %s: %s", ride_id, e)


@router.post("/process/{ride_id}", response_model=ProcessResponse)
@limiter.limit(PROCESS_LIMIT)
async def process_ride(
    request: Request,
    ride_id: str,
    authorization: str = Header(None),
):
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

    result = svc.client.table("rides_metadata").update({"status": "processing"}).eq("id", ride_id).eq("status", current_status).execute()
    if not result.data:
        raise HTTPException(status_code=409, detail="Ride status changed, please retry")

    await submit_background_task(_run_process_async(ride_id), name=f"process_{ride_id}")

    return ProcessResponse(
        status="accepted",
        ride_id=ride_id,
        message="Processing started in background",
    )
