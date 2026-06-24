from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import logging
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas
from dotenv import load_dotenv
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_retry_strategy = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

load_dotenv()

CONTAINER_NAME = "raw-road-data"
SAS_EXPIRY_MINUTES = 15


class BlobStorageService:
    """Encapsulates Azure Blob Storage operations."""

    def __init__(self, connection_string: str | None = None) -> None:
        self._conn_str = (connection_string or os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
        self._client: BlobServiceClient | None = None

    @property
    def client(self) -> BlobServiceClient:
        if self._client is None:
            if not self._conn_str:
                raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must be set in environment")
            self._client = BlobServiceClient.from_connection_string(self._conn_str)
        return self._client

    # ---- download ----

    @_retry_strategy
    def download_file(self, object_path: str, container: str = CONTAINER_NAME) -> bytes:
        blob_client = self.client.get_blob_client(container=container, blob=object_path)
        download_stream = blob_client.download_blob(timeout=120)
        return download_stream.readall()

    # ---- upload ----

    @_retry_strategy
    def upload_bytes(
        self,
        object_path: str,
        data: bytes,
        content_type: str,
        container: str = CONTAINER_NAME,
    ) -> None:
        blob_client = self.client.get_blob_client(container=container, blob=object_path)
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings={"content_type": content_type},
        )

    def upload_with_signed_url(
        self,
        object_path: str,
        data: bytes,
        content_type: str,
        container: str = CONTAINER_NAME,
    ) -> None:
        """Upload using a signed URL (for Supabase storage compatibility)."""
        from types import SimpleNamespace

        bucket = self.client.get_container_client(container)
        signed_upload = bucket.create_signed_upload_url(
            object_path,
            SimpleNamespace(upsert=True),
        )
        bucket.upload_to_signed_url(
            object_path,
            signed_upload["token"],
            data,
            {"content-type": content_type, "x-upsert": "true"},
        )

    # ---- delete ----

    def delete_blobs(self, paths: list[str], container: str = CONTAINER_NAME) -> None:
        container_client = self.client.get_container_client(container)
        for path in paths:
            try:
                container_client.delete_blob(path, timeout=10)
            except Exception:
                pass

    # ---- SAS URL generation ----

    @_retry_strategy
    def generate_sas_url(
        self,
        blob_path: str,
        content_type: str,
        container: str = CONTAINER_NAME,
    ) -> tuple[str, datetime]:
        blob_client = self.client.get_blob_client(container=container, blob=blob_path)
        expiry_time = datetime.now(timezone.utc) + timedelta(minutes=SAS_EXPIRY_MINUTES)

        sas_token = generate_blob_sas(
            account_name=blob_client.account_name,
            container_name=container,
            blob_name=blob_path,
            account_key=blob_client.credential.account_key,
            permission=BlobSasPermissions(write=True, create=True),
            expiry=expiry_time,
            content_type=content_type,
        )

        sas_url = f"{blob_client.url}?{sas_token}"
        return sas_url, expiry_time

    # ---- validation ----

    EXPECTED_CONTENT_TYPES: dict[str, str] = {
        "video": "video/mp4",
        "GPS": "application/json",
    }
    _MP4_HEADER_BYTES = 8

    def validate_blob_content(
        self,
        object_path: str,
        label: str,
        container: str = CONTAINER_NAME,
    ) -> None:
        """Validate a blob exists, has correct content type, and valid data."""
        blob_client = self.client.get_blob_client(container=container, blob=object_path)
        props = blob_client.get_blob_properties()

        if props.size == 0:
            raise HTTPException(
                status_code=400,
                detail=f"{label} blob is empty (0 bytes). Upload may have failed.",
            )

        expected_type = self.EXPECTED_CONTENT_TYPES[label]
        stored_type = (props.content_settings.content_type if props.content_settings else None)
        if not stored_type:
            raise HTTPException(
                status_code=400,
                detail=f"{label} blob has no content type set",
            )
        if stored_type != expected_type:
            raise HTTPException(
                status_code=400,
                detail=f"{label} blob has incorrect content type '{stored_type}', expected '{expected_type}'",
            )

        if label == "video":
            stream = blob_client.download_blob(offset=0, length=self._MP4_HEADER_BYTES)
            header = stream.readall()
            if len(header) < self._MP4_HEADER_BYTES or header[4:8] != b"ftyp":
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} blob is not a valid MP4 (missing ftyp box)",
                )
        elif label == "GPS":
            stream = blob_client.download_blob(offset=0, length=256)
            data = stream.readall()
            stripped = data.lstrip()
            if not stripped or stripped[0] not in (ord("{"), ord("[")):
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} blob is not valid JSON",
                )


# ---- module-level singleton for backward compatibility ----

_default_service: BlobStorageService | None = None


def get_blob_storage_service() -> BlobStorageService:
    global _default_service
    if _default_service is None:
        _default_service = BlobStorageService()
    return _default_service
