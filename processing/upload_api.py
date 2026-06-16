import uuid
from datetime import datetime, timedelta, timezone

from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from .common import (
    CONTAINER_NAME,
    SAS_EXPIRY_MINUTES,
    _get_blob_service,
    _get_supabase,
    _validate_token,
)
from .rate_limiter import UPLOAD_LIMIT, limiter

router = APIRouter(prefix="/upload", tags=["upload"])


class InitUploadRequest(BaseModel):
    video_filename: str
    gps_filename: str


class InitUploadResponse(BaseModel):
    ride_id: str
    video_sas_url: str
    gps_sas_url: str
    video_path: str
    gps_path: str
    expires_at: str


class CompleteUploadRequest(BaseModel):
    ride_id: str
    video_path: str
    gps_path: str


class AbortUploadRequest(BaseModel):
    video_path: str
    gps_path: str


def _generate_sas_url(blob_path: str, content_type: str) -> tuple[str, datetime]:
    blob_service = _get_blob_service()
    blob_client = blob_service.get_blob_client(container=CONTAINER_NAME, blob=blob_path)

    expiry_time = datetime.now(timezone.utc) + timedelta(minutes=SAS_EXPIRY_MINUTES)

    sas_token = generate_blob_sas(
        account_name=blob_client.account_name,
        container_name=CONTAINER_NAME,
        blob_name=blob_path,
        account_key=blob_client.credential.account_key,
        permission=BlobSasPermissions(write=True, create=True),
        expiry=expiry_time,
        content_type=content_type,
    )

    sas_url = f"{blob_client.url}?{sas_token}"
    return sas_url, expiry_time


def _delete_blobs(paths: list[str]) -> None:
    blob_service = _get_blob_service()
    container_client = blob_service.get_container_client(CONTAINER_NAME)
    for path in paths:
        try:
            container_client.delete_blob(path, timeout=10)
        except Exception:
            pass


def _validate_path_ownership(user_id: str, paths: list[str]) -> None:
    for path in paths:
        if not path.startswith(f"{user_id}/"):
            raise HTTPException(status_code=403, detail="Path does not belong to you")


@router.post("/init", response_model=InitUploadResponse)
@limiter.limit(UPLOAD_LIMIT)
async def init_upload(
    request: Request, upload_request: InitUploadRequest, authorization: str = Header(None)
):
    auth = _validate_token(authorization)
    user_id = auth["user_id"]

    ride_id = str(uuid.uuid4())
    video_path = f"{user_id}/{ride_id}.mp4"
    gps_path = f"{user_id}/{ride_id}.json"

    video_sas_url, expires_at = _generate_sas_url(video_path, "video/mp4")
    gps_sas_url, _ = _generate_sas_url(gps_path, "application/json")

    return InitUploadResponse(
        ride_id=ride_id,
        video_sas_url=video_sas_url,
        gps_sas_url=gps_sas_url,
        video_path=video_path,
        gps_path=gps_path,
        expires_at=expires_at.isoformat(),
    )


@router.post("/complete")
@limiter.limit(UPLOAD_LIMIT)
async def complete_upload(
    request: Request, body: CompleteUploadRequest, authorization: str = Header(None)
):
    auth = _validate_token(authorization)
    user_id = auth["user_id"]

    _validate_path_ownership(user_id, [body.video_path, body.gps_path])

    blob_service = _get_blob_service()
    for path, label in [(body.video_path, "video"), (body.gps_path, "GPS")]:
        bc = blob_service.get_blob_client(container=CONTAINER_NAME, blob=path)
        props = bc.get_blob_properties()
        if props.size == 0:
            raise HTTPException(
                status_code=400,
                detail=f"{label} blob is empty (0 bytes). Upload may have failed.",
            )

    supabase = _get_supabase()
    supabase.table("rides_metadata").insert(
        {
            "id": body.ride_id,
            "user_id": user_id,
            "video_bucket_path": body.video_path,
            "gps_bucket_path": body.gps_path,
            "status": "queued",
        }
    ).execute()

    return {"status": "ok", "ride_id": body.ride_id}


@router.post("/abort")
@limiter.limit(UPLOAD_LIMIT)
async def abort_upload(
    request: Request, body: AbortUploadRequest, authorization: str = Header(None)
):
    auth = _validate_token(authorization)
    user_id = auth["user_id"]
    _validate_path_ownership(user_id, [body.video_path, body.gps_path])
    _delete_blobs([body.video_path, body.gps_path])
    return {"status": "ok", "message": "Uploaded blobs deleted"}
