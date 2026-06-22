import re
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from .rate_limiter import UPLOAD_LIMIT, limiter
from .services.blob_storage import BlobStorageService, get_blob_storage_service
from .services.supabase_client import get_supabase_service

router = APIRouter(prefix="/upload", tags=["upload"])

_BLOB_PATH_RE = re.compile(
    r"^[a-zA-Z0-9_.@-]+/"
    r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
    r"\.(mp4|json)$"
)


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


def _validate_path_ownership(user_id: str, paths: list[str]) -> None:
    for path in paths:
        if not path.startswith(f"{user_id}/"):
            raise HTTPException(status_code=403, detail="Path does not belong to you")


def _validate_blob_path(path: str) -> None:
    if not path:
        raise HTTPException(status_code=400, detail="Blob path is empty")
    if ".." in path.replace("\\", "/").split("/"):
        raise HTTPException(
            status_code=400, detail="Blob path contains directory traversal"
        )
    if not _BLOB_PATH_RE.match(path):
        raise HTTPException(status_code=400, detail="Blob path format is invalid")


@router.post("/init", response_model=InitUploadResponse)
@limiter.limit(UPLOAD_LIMIT)
async def init_upload(
    request: Request, upload_request: InitUploadRequest, authorization: str = Header(None)
):
    supabase = get_supabase_service()
    blob = get_blob_storage_service()

    auth = supabase.validate_token(authorization)
    user_id = auth["user_id"]

    ride_id = str(uuid.uuid4())
    video_path = f"{user_id}/{ride_id}.mp4"
    gps_path = f"{user_id}/{ride_id}.json"

    video_sas_url, expires_at = blob.generate_sas_url(video_path, "video/mp4")
    gps_sas_url, _ = blob.generate_sas_url(gps_path, "application/json")

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
    supabase = get_supabase_service()
    blob = get_blob_storage_service()

    auth = supabase.validate_token(authorization)
    user_id = auth["user_id"]

    for path in [body.video_path, body.gps_path]:
        _validate_blob_path(path)
    _validate_path_ownership(user_id, [body.video_path, body.gps_path])

    for label, path in [("video", body.video_path), ("GPS", body.gps_path)]:
        blob.validate_blob_content(path, label)

    supabase.insert(
        "rides_metadata",
        {
            "id": body.ride_id,
            "user_id": user_id,
            "video_bucket_path": body.video_path,
            "gps_bucket_path": body.gps_path,
            "status": "queued",
        },
    )

    return {"status": "ok", "ride_id": body.ride_id}


@router.post("/abort")
@limiter.limit(UPLOAD_LIMIT)
async def abort_upload(
    request: Request, body: AbortUploadRequest, authorization: str = Header(None)
):
    supabase = get_supabase_service()
    blob = get_blob_storage_service()

    auth = supabase.validate_token(authorization)
    user_id = auth["user_id"]
    for path in [body.video_path, body.gps_path]:
        _validate_blob_path(path)
    _validate_path_ownership(user_id, [body.video_path, body.gps_path])
    blob.delete_blobs([body.video_path, body.gps_path])
    return {"status": "ok", "message": "Uploaded blobs deleted"}
