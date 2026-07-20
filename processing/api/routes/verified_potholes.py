from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from ...rate_limiter import READ_LIMIT, limiter
from ...services.supabase_client import get_supabase_service

router = APIRouter(tags=["verified-potholes"])


class CaptionUpdate(BaseModel):
    caption: str


@router.put("/verified-potholes/{pothole_id}")
@limiter.limit(READ_LIMIT)
async def update_pothole_caption(
    request: Request,
    pothole_id: int,
    body: CaptionUpdate,
    authorization: str = Header(None),
):
    svc = get_supabase_service()
    auth = svc.validate_token(authorization)

    rows = svc.select("verified_potholes", "id,ride_id", id=pothole_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Pothole not found")

    pothole = rows[0]
    ride_id = pothole.get("ride_id")
    if ride_id:
        ride_rows = svc.select("rides_metadata", "user_id", id=ride_id)
        if ride_rows and ride_rows[0].get("user_id") != auth.get("user_id"):
            raise HTTPException(status_code=403, detail="You can only edit captions for your own detections")

    svc.update("verified_potholes", {"caption": body.caption}, id=pothole_id)
    return {"status": "ok", "caption": body.caption}
