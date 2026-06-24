from fastapi import APIRouter, Header, HTTPException, Query
from starlette.requests import Request

from ...rate_limiter import DELETE_LIMIT, READ_LIMIT, limiter
from ...services.supabase_client import get_supabase_service

router = APIRouter(tags=["rides"])


@router.get("/rides")
@limiter.limit(READ_LIMIT)
async def list_rides(
    request: Request,
    authorization: str = Header(None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    svc = get_supabase_service()
    auth = svc.validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    columns = "id,user_id,video_bucket_path,gps_bucket_path,status,error_log,created_at"
    query = svc.table("rides_metadata").select(columns, count="exact").eq("user_id", user_id)
    result = query.range(offset, offset + limit - 1).execute()

    rows = result.data or []
    total = result.count or 0

    return {
        "rides": rows,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
        },
    }


@router.get("/rides/{ride_id}")
@limiter.limit(READ_LIMIT)
async def get_ride(request: Request, ride_id: str, authorization: str = Header(None)):
    svc = get_supabase_service()
    auth = svc.validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    rows = svc.select("rides_metadata", "*", id=ride_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Ride not found")

    if rows[0].get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not your ride")

    return {"ride": rows[0]}


@router.delete("/rides/{ride_id}")
@limiter.limit(DELETE_LIMIT)
async def delete_ride(request: Request, ride_id: str, authorization: str = Header(None)):
    svc = get_supabase_service()
    auth = svc.validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    rows = svc.select("rides_metadata", "user_id", id=ride_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Ride not found")
    if rows[0]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your ride")

    svc.delete("rides_metadata", id=ride_id)
    return {"status": "deleted"}
