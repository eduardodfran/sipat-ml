import threading
import traceback

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .common import _get_supabase, _validate_token
from .upload_api import router as upload_router

app = FastAPI(title="SIPAT Process API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload_router)


class ProcessResponse(BaseModel):
    status: str
    ride_id: str
    message: str


class RideInfo(BaseModel):
    id: str
    user_id: str | None
    video_bucket_path: str | None
    gps_bucket_path: str | None
    status: str
    error_log: str | None
    created_at: str | None


def _run_process(ride_id: str) -> None:
    from .batch_worker import (
        _build_supabase_client,
        _friendly_error_message,
        _mark_failed,
        _process_ride,
    )

    try:
        supabase = _build_supabase_client()
        response = supabase.table("rides_metadata").select("*").eq("id", ride_id).execute()
        rows = response.data or []
        if not rows:
            print(f"Ride {ride_id} not found")
            return
        ride = rows[0]
        ride["status"] = "processing"
        try:
            result = _process_ride(supabase, ride)
            print(f"Completed ride {ride_id}: {result['raw_detection_count']} detections")
        except Exception as exc:
            error_message = _friendly_error_message(exc)
            traceback_text = traceback.format_exc()
            combined_error = f"{error_message}\n{traceback_text}"
            try:
                _mark_failed(supabase, ride_id, combined_error)
            except Exception as mark_exc:
                print(f"Failed to mark ride {ride_id} as failed: {mark_exc}")
            print(f"Background processing failed for ride {ride_id}: {exc}")
    except Exception as e:
        print(f"Setup error in background processing for ride {ride_id}: {e}")


@app.post("/process/{ride_id}", response_model=ProcessResponse)
async def process_ride(ride_id: str, authorization: str = Header(None)):
    auth = _validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    supabase = _get_supabase()
    response = supabase.table("rides_metadata").select("id,user_id,status").eq("id", ride_id).execute()
    rows = response.data or []
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

    supabase.table("rides_metadata").update({"status": "processing"}).eq("id", ride_id).execute()

    thread = threading.Thread(target=_run_process, args=(ride_id,), daemon=True)
    thread.start()

    return ProcessResponse(
        status="accepted",
        ride_id=ride_id,
        message="Processing started in background",
    )


@app.get("/rides")
async def list_rides(authorization: str = Header(None)):
    auth = _validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")
    supabase = _get_supabase()
    response = (
        supabase.table("rides_metadata")
        .select("id,user_id,video_bucket_path,gps_bucket_path,status,error_log,created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"rides": response.data or []}


@app.get("/rides/{ride_id}")
async def get_ride(ride_id: str, authorization: str = Header(None)):
    auth = _validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    supabase = _get_supabase()
    response = supabase.table("rides_metadata").select("*").eq("id", ride_id).execute()
    rows = response.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Ride not found")

    if rows[0].get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not your ride")

    return {"ride": rows[0]}


@app.delete("/rides/{ride_id}")
async def delete_ride(ride_id: str, authorization: str = Header(None)):
    auth = _validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")
    supabase = _get_supabase()
    ride = supabase.table("rides_metadata").select("user_id,video_bucket_path,gps_bucket_path").eq("id", ride_id).execute()
    if not ride.data:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.data[0]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your ride")
    supabase.table("rides_metadata").delete().eq("id", ride_id).execute()
    return {"status": "deleted"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
