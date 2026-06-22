import threading
import traceback

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from ...rate_limiter import PROCESS_LIMIT, limiter
from ...services.supabase_client import get_supabase_service

router = APIRouter(tags=["process"])


class ProcessResponse(BaseModel):
    status: str
    ride_id: str
    message: str


def _run_process(ride_id: str) -> None:
    from ...pipeline.worker import RideProcessor

    try:
        svc = get_supabase_service()
        rows = svc.select("rides_metadata", "*", id=ride_id)
        if not rows:
            print(f"Ride {ride_id} not found")
            return
        ride = rows[0]
        ride["status"] = "processing"
        try:
            processor = RideProcessor()
            result = processor.process_ride(ride)
            print(f"Completed ride {ride_id}: {result['raw_detection_count']} detections")
        except Exception as exc:
            error_message = str(exc)
            traceback_text = traceback.format_exc()
            try:
                svc.update("rides_metadata", {"status": "failed", "error_log": error_message}, id=ride_id)
            except Exception as mark_exc:
                print(f"Failed to mark ride {ride_id} as failed: {mark_exc}")
            print(f"Background processing failed for ride {ride_id}: {exc}")
            print(traceback_text)
    except Exception as e:
        print(f"Setup error in background processing for ride {ride_id}: {e}")


@router.post("/process/{ride_id}", response_model=ProcessResponse)
@limiter.limit(PROCESS_LIMIT)
async def process_ride(
    request: Request, ride_id: str, authorization: str = Header(None)
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

    svc.update("rides_metadata", {"status": "processing"}, id=ride_id)

    thread = threading.Thread(target=_run_process, args=(ride_id,), daemon=True)
    thread.start()

    return ProcessResponse(
        status="accepted",
        ride_id=ride_id,
        message="Processing started in background",
    )
