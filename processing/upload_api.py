import uuid
from datetime import datetime, timedelta, timezone

from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .common import (
    CONTAINER_NAME,
    SAS_EXPIRY_MINUTES,
    _get_blob_service,
    _get_supabase,
    _validate_token,
)

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


@router.post("/init", response_model=InitUploadResponse)
async def init_upload(request: InitUploadRequest, authorization: str = Header(None)):
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
async def complete_upload(
    request: CompleteUploadRequest, authorization: str = Header(None)
):
    auth = _validate_token(authorization)
    user_id = auth["user_id"]

    blob_service = _get_blob_service()
    for path, label in [(request.video_path, "video"), (request.gps_path, "GPS")]:
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
            "id": request.ride_id,
            "user_id": user_id,
            "video_bucket_path": request.video_path,
            "gps_bucket_path": request.gps_path,
            "status": "queued",
        }
    ).execute()

    return {"status": "ok", "ride_id": request.ride_id}


@router.post("/abort")
async def abort_upload(
    request: AbortUploadRequest, authorization: str = Header(None)
):
    _validate_token(authorization)
    _delete_blobs([request.video_path, request.gps_path])
    return {"status": "ok", "message": "Uploaded blobs deleted"}
