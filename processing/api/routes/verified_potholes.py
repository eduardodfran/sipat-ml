from fastapi import APIRouter, Header, HTTPException
from starlette.requests import Request

from ...rate_limiter import READ_LIMIT, limiter
from ...services.supabase_client import get_supabase_service
from .post_text import PostTextUpdate, log_text_edit, normalize_text_update

router = APIRouter(tags=["verified-potholes"])


@router.put("/verified-potholes/{pothole_id}")
@limiter.limit(READ_LIMIT)
async def update_pothole_text(
    request: Request,
    pothole_id: int,
    body: PostTextUpdate,
    authorization: str = Header(None),
):
    """Owner-only edit of title/description/caption (first detector = ride owner).

    `caption` is still accepted for backward compatibility with older clients.
    """
    svc = get_supabase_service()
    auth = svc.validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    rows = svc.select("verified_potholes", "id,ride_id,caption,title,description", id=pothole_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Pothole not found")

    pothole = rows[0]
    # Ownership must be provable via the first-detector ride. Orphan potholes
    # (no ride, or ride row gone) are denied to everyone — including reporters.
    ride_id = pothole.get("ride_id")
    ride_owner = None
    if ride_id:
        ride_rows = svc.select("rides_metadata", "user_id", id=ride_id)
        if ride_rows:
            ride_owner = ride_rows[0].get("user_id")
    if ride_owner != user_id:
        raise HTTPException(status_code=403, detail="You can only edit titles and descriptions for your own detections")

    updates = normalize_text_update(body)
    if not updates:
        raise HTTPException(status_code=422, detail="No editable fields provided")

    log_text_edit(svc, post_type="pothole", post_id=pothole_id, user_id=user_id, old=pothole, new_updates=updates)
    svc.update("verified_potholes", updates, id=pothole_id)
    return {"status": "ok", **updates}
