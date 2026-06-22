"""
Backward-compatibility shim — delegates to services package.

New code should import directly from services.supabase_client / services.blob_storage.
"""

from typing import Any

from .services.blob_storage import BlobStorageService, get_blob_storage_service
from .services.supabase_client import SupabaseService, get_supabase_service

# re-export constants for any code still importing from common
from .services.blob_storage import CONTAINER_NAME, SAS_EXPIRY_MINUTES


def _get_supabase():
    return get_supabase_service().client


def _get_blob_service():
    return get_blob_storage_service().client


def _validate_token(authorization: str | None) -> dict[str, Any]:
    return get_supabase_service().validate_token(authorization)
