import os
from typing import Any

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from fastapi import HTTPException
from supabase import Client, create_client

load_dotenv()

CONTAINER_NAME = "raw-road-data"
SAS_EXPIRY_MINUTES = 15

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

_blob_service: BlobServiceClient | None = None
_supabase: Client | None = None


def _get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        if not SUPABASE_URL or not SERVICE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment")
        _supabase = create_client(SUPABASE_URL, SERVICE_KEY)
    return _supabase


def _get_blob_service() -> BlobServiceClient:
    global _blob_service
    if _blob_service is None:
        if not AZURE_CONNECTION_STRING:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must be set in environment")
        _blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    return _blob_service


def _validate_token(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    supabase = _get_supabase()
    try:
        response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not response.user or response.user.id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {"user_id": response.user.id}
