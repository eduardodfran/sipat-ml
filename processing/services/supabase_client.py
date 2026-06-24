from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import urlparse

import logging
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from dotenv import load_dotenv
from fastapi import HTTPException
from supabase import Client, create_client

logger = logging.getLogger(__name__)

_retry_strategy = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

load_dotenv()


class SupabaseService:
    """Encapsulates Supabase client creation, config validation, and auth."""

    def __init__(
        self,
        url: str | None = None,
        service_key: str | None = None,
    ) -> None:
        self._url = (url or os.getenv("SUPABASE_URL") or "").strip()
        self._service_key = (service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        self._client: Client | None = None
        self._validate_config()

    # ---- properties ----

    @property
    def client(self) -> Client:
        if self._client is None:
            if not self._url or not self._service_key:
                raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
            self._client = create_client(self._url, self._service_key)
        return self._client

    @property
    def url(self) -> str:
        return self._url

    # ---- config validation ----

    @staticmethod
    def _project_ref_from_url(url: str) -> str | None:
        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            return None
        if hostname.endswith(".supabase.co"):
            return hostname.split(".")[0]
        return None

    @staticmethod
    def _jwt_payload(token: str) -> dict[str, Any] | None:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        try:
            payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _validate_config(self) -> None:
        if self._service_key.count(".") < 2:
            raise ValueError(
                "SUPABASE_SERVICE_ROLE_KEY does not look like a Supabase JWT "
                "(expected 3 dot-separated segments)"
            )
        url_ref = self._project_ref_from_url(self._url)
        payload = self._jwt_payload(self._service_key) or {}
        key_ref = payload.get("ref") if isinstance(payload.get("ref"), str) else None
        role = payload.get("role") if isinstance(payload.get("role"), str) else None

        if role and role != "service_role":
            raise ValueError(
                f"SUPABASE_SERVICE_ROLE_KEY is not a service_role token (role={role!r}). "
                "Paste the service_role key from Supabase Dashboard -> Project Settings -> API."
            )
        if url_ref and key_ref and url_ref != key_ref:
            raise ValueError(
                f"Supabase project mismatch: your SUPABASE_URL points to a different project "
                f"than your SUPABASE_SERVICE_ROLE_KEY.\n"
                f"   - URL project ref: {url_ref}\n"
                f"   - Key project ref: {key_ref}\n"
                "Fix: Open Supabase Dashboard -> Project Settings -> API for the URL's project, "
                "then copy its service_role key into sipat-ml/.env."
            )

    # ---- auth ----

    def validate_token(self, authorization: str | None) -> dict[str, Any]:
        """Validate a Bearer token and return the user payload."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

        token = authorization.removeprefix("Bearer ").strip()
        try:
            response = self.client.auth.get_user(token)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        if not response.user or response.user.id is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        return {"user_id": response.user.id}

    # ---- table helpers ----

    def table(self, name: str):
        """Return a table query builder."""
        return self.client.table(name)

    @_retry_strategy
    def insert(self, table: str, data: dict | list[dict]) -> Any:
        return self.client.table(table).insert(data).execute()

    @_retry_strategy
    def update(self, table: str, data: dict, **filters) -> Any:
        query = self.client.table(table).update(data)
        for key, value in filters.items():
            query = query.eq(key, value)
        return query.execute()

    @_retry_strategy
    def delete(self, table: str, **filters) -> Any:
        query = self.client.table(table).delete()
        for key, value in filters.items():
            query = query.eq(key, value)
        return query.execute()

    @_retry_strategy
    def select(self, table: str, columns: str = "*", **filters) -> list[dict]:
        query = self.client.table(table).select(columns)
        for key, value in filters.items():
            query = query.eq(key, value)
        return (query.execute().data) or []

    @_retry_strategy
    def upsert(self, table: str, data: dict | list[dict]) -> Any:
        return self.client.table(table).upsert(data).execute()

    # ---- storage helpers ----

    @_retry_strategy
    def storage_download(self, bucket: str, path: str) -> bytes:
        return self.client.storage.from_(bucket).download(path)

    @_retry_strategy
    def storage_upload(
        self,
        bucket: str,
        path: str,
        file_bytes: bytes,
        content_type: str,
        upsert: bool = True,
    ) -> Any:
        opts = {"content-type": content_type}
        if upsert:
            opts["upsert"] = "true"
        return self.client.storage.from_(bucket).upload(
            path=path, file=file_bytes, file_options=opts
        )

    def storage_get_public_url(self, bucket: str, path: str) -> str:
        return f"{self._url}/storage/v1/object/public/{bucket}/{path}"


# ---- module-level singleton for backward compatibility ----

_default_service: SupabaseService | None = None


def get_supabase_service() -> SupabaseService:
    global _default_service
    if _default_service is None:
        _default_service = SupabaseService()
    return _default_service
