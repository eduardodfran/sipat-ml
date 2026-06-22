from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from postgrest.exceptions import APIError
from storage3.utils import StorageException
from supabase import Client, create_client

CURRENT_DIR = Path(__file__).resolve().parent
ENV_PATH = CURRENT_DIR.parent / ".env"

load_dotenv(dotenv_path=ENV_PATH)

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

BUCKET_NAME = "raw-road-data"
TEST_USER_ID = "test_user"
SIMULATION_BASENAME = "simulation_run"
DEFAULT_SOURCE_VIDEO_OBJECT = os.getenv("SOURCE_VIDEO_OBJECT_PATH", "").strip()
DEFAULT_SOURCE_GPS_OBJECT = os.getenv("SOURCE_GPS_OBJECT_PATH", "").strip()


def _supabase_project_ref_from_url(url: str) -> str | None:
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return None

    if hostname.endswith(".supabase.co"):
        return hostname.split(".")[0]
    return None


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


def _validate_supabase_config(url: str, key: str) -> None:
    if key.count(".") < 2:
        raise ValueError(
            "SUPABASE_SERVICE_ROLE_KEY does not look like a Supabase JWT (expected 3 dot-separated segments)."
        )

    url_ref = _supabase_project_ref_from_url(url)
    payload = _jwt_payload(key) or {}
    key_ref = payload.get("ref") if isinstance(payload.get("ref"), str) else None
    role = payload.get("role") if isinstance(payload.get("role"), str) else None

    if role and role != "service_role":
        raise ValueError(
            f"SUPABASE_SERVICE_ROLE_KEY is not a service_role token (role={role!r})."
        )

    if url_ref and key_ref and url_ref != key_ref:
        raise ValueError(
            "Supabase project mismatch: SUPABASE_URL points to a different project than SUPABASE_SERVICE_ROLE_KEY."
        )


def _build_supabase_client() -> Client:
    if not SUPABASE_URL or not SERVICE_KEY:
        raise ValueError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in sipat-ml/.env"
        )

    _validate_supabase_config(SUPABASE_URL, SERVICE_KEY)
    return create_client(SUPABASE_URL, SERVICE_KEY)


def _read_json_text(json_text: str, source_name: str) -> list[dict[str, Any]]:
    payload = json.loads(json_text)

    if not isinstance(payload, list):
        raise ValueError(f"{source_name} must contain a JSON array")

    return payload


def _read_json_file(file_path: Path) -> list[dict[str, Any]]:
    with file_path.open("r", encoding="utf-8") as file_handle:
        payload = json.load(file_handle)

    if not isinstance(payload, list):
        raise ValueError(f"{file_path.name} must contain a JSON array")

    return payload


def _read_csv_file(file_path: Path) -> list[dict[str, Any]]:
    with file_path.open("r", encoding="utf-8", newline="") as file_handle:
        reader = csv.DictReader(file_handle)
        rows = list(reader)

    if not rows:
        raise ValueError(f"{file_path.name} must contain at least one row")

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            normalized_rows.append(
                {
                    "timestamp_seconds": float(row["timestamp_seconds"]),
                    "lat": float(row["lat"]),
                    "lng": float(row["lng"]),
                }
            )
        except KeyError as exc:
            raise ValueError(
                f"{file_path.name} must contain timestamp_seconds, lat, and lng columns"
            ) from exc
        except ValueError as exc:
            raise ValueError(
                f"Row {index + 1} in {file_path.name} contains non-numeric GPS values"
            ) from exc

    return normalized_rows


def _read_csv_text(csv_text: str, source_name: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(csv_text.splitlines())
    rows = list(reader)

    if not rows:
        raise ValueError(f"{source_name} must contain at least one row")

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            normalized_rows.append(
                {
                    "timestamp_seconds": float(row["timestamp_seconds"]),
                    "lat": float(row["lat"]),
                    "lng": float(row["lng"]),
                }
            )
        except KeyError as exc:
            raise ValueError(
                f"{source_name} must contain timestamp_seconds, lat, and lng columns"
            ) from exc
        except ValueError as exc:
            raise ValueError(
                f"Row {index + 1} in {source_name} contains non-numeric GPS values"
            ) from exc

    return normalized_rows


def _normalize_remote_path(remote_path: str, label: str) -> str:
    normalized = remote_path.strip().lstrip("/")
    if not normalized:
        raise ValueError(f"Missing {label} in Supabase storage")
    if normalized.startswith(f"{BUCKET_NAME}/"):
        normalized = normalized[len(BUCKET_NAME) + 1 :]
    return normalized


def _download_source_object(supabase: Client, object_path: str) -> bytes:
    response = supabase.storage.from_(BUCKET_NAME).download(object_path)
    if not response:
        raise RuntimeError(f"No bytes returned when downloading {object_path}")
    return response


def _upload_bytes_with_overwrite(
    supabase: Client,
    object_path: str,
    file_payload: bytes,
    content_type: str,
) -> None:
    bucket = supabase.storage.from_(BUCKET_NAME)
    signed_upload = bucket.create_signed_upload_url(
        object_path,
        SimpleNamespace(upsert=True),
    )
    response = bucket.upload_to_signed_url(
        object_path,
        signed_upload["token"],
        file_payload,
        {
            "content-type": content_type,
            "x-upsert": "true",
        },
    )

    if not response:
        raise RuntimeError(f"Upload returned no response for {object_path}")


def _insert_ride_metadata(
    supabase: Client,
    ride_id: str,
    video_bucket_path: str,
    gps_bucket_path: str,
) -> None:
    supabase.schema("public").from_("rides_metadata").insert(
        {
            "id": ride_id,
            "user_id": TEST_USER_ID,
            "video_bucket_path": video_bucket_path,
            "gps_bucket_path": gps_bucket_path,
            "status": "pending",
        }
    ).execute()


def trigger_test_ingest() -> dict[str, str]:
    supabase = _build_supabase_client()
    parser = argparse.ArgumentParser(
        description="Copy a source video and GPS telemetry file from Supabase storage into the simulation paths for batch worker testing."
    )
    parser.add_argument(
        "--source-video-object-path",
        dest="source_video_object_path",
        default=None,
        help="Source .mp4 object path in Supabase storage (inside raw-road-data or relative to it).",
    )
    parser.add_argument(
        "--source-gps-object-path",
        dest="source_gps_object_path",
        default=None,
        help="Source .json or .csv object path in Supabase storage (inside raw-road-data or relative to it).",
    )
    args = parser.parse_args()

    source_video_object_path = _normalize_remote_path(
        args.source_video_object_path or DEFAULT_SOURCE_VIDEO_OBJECT,
        "source video object path",
    )
    source_gps_object_path = _normalize_remote_path(
        args.source_gps_object_path or DEFAULT_SOURCE_GPS_OBJECT,
        "source GPS object path",
    )

    ride_id = str(uuid.uuid4())
    video_bucket_path = f"{BUCKET_NAME}/{TEST_USER_ID}/{SIMULATION_BASENAME}.mp4"
    gps_bucket_path = f"{BUCKET_NAME}/{TEST_USER_ID}/{SIMULATION_BASENAME}.json"

    uploaded_paths: list[str] = []

    try:
        source_video_bytes = _download_source_object(supabase, source_video_object_path)
        _upload_bytes_with_overwrite(
            supabase,
            video_bucket_path,
            source_video_bytes,
            "video/mp4",
        )
        uploaded_paths.append(video_bucket_path)

        source_gps_bytes = _download_source_object(supabase, source_gps_object_path)
        if source_gps_object_path.lower().endswith(".csv"):
            gps_payload = _read_csv_text(
                source_gps_bytes.decode("utf-8"), source_gps_object_path
            )
        else:
            gps_payload = _read_json_text(
                source_gps_bytes.decode("utf-8"), source_gps_object_path
            )

        _upload_bytes_with_overwrite(
            supabase,
            gps_bucket_path,
            json.dumps(gps_payload, separators=(",", ":")).encode("utf-8"),
            "application/json",
        )
        uploaded_paths.append(gps_bucket_path)

        _insert_ride_metadata(supabase, ride_id, video_bucket_path, gps_bucket_path)

        return {
            "ride_id": ride_id,
            "user_id": TEST_USER_ID,
            "source_video_object_path": source_video_object_path,
            "source_gps_object_path": source_gps_object_path,
            "video_bucket_path": video_bucket_path,
            "gps_bucket_path": gps_bucket_path,
        }
    except (APIError, StorageException, OSError, ValueError, KeyError) as exc:
        if uploaded_paths:
            try:
                supabase.storage.from_(BUCKET_NAME).remove(uploaded_paths)
            except Exception as cleanup_error:
                print(f"Cleanup warning: failed to remove uploaded files: {cleanup_error}")

        traceback_text = traceback.format_exc()
        raise RuntimeError(f"Test ingest trigger failed: {exc}\n{traceback_text}") from exc


if __name__ == "__main__":
    result = trigger_test_ingest()
    print(json.dumps(result, indent=2))
