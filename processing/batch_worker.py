import base64
import json
import os
import subprocess
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from postgrest.exceptions import APIError
from storage3.utils import StorageException
from supabase import Client, create_client
from ultralytics import YOLO

from .clustering import cluster_pothole_detections
from .detection_batch_builder import DetectionBatchBuilder
from .utils.geo_math import haversine_distance_meters
from .utils.gps_processor import GPSProcessor

CURRENT_DIR = Path(__file__).resolve().parent
ENV_PATH = CURRENT_DIR.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

MODEL_PATH = CURRENT_DIR.parent / "weights" / "best.pt"
MERGE_RADIUS_METERS = 15.0

# DPWH D.O. No. 120 s. 2019 adopts FHWA LTPP Distress ID Manual for pothole
# severity, which classifies by depth (<25mm Low, 25-50mm Moderate, >50mm High).
# Since only plan area is available, map via PAVER (US Army) combined diameter/depth
# matrix: 200mm diam (~0.03m^2) and 460mm diam (~0.17m^2) boundaries.
# DPWH minimum documented pothole area: ~0.02m^2 (FHWA min plan dimension 150mm).
# Source: FHWA-RD-03-031 LTPP Distress ID Manual §8 Potholes;
#         PAVER Road Asphalt Distress Manual §13 Potholes Table 1.
SEVERITY_MINOR_AREA_M2 = 0.03
SEVERITY_MODERATE_AREA_M2 = 0.17

CONFIDENCE_MODERATE_CAP = 0.35
CONFIDENCE_SEVERE_CAP = 0.50


def _phys_area_to_severity(area_m2: float | None) -> str:
    if area_m2 is None or area_m2 < SEVERITY_MINOR_AREA_M2:
        return "Minor"
    if area_m2 < SEVERITY_MODERATE_AREA_M2:
        return "Moderate"
    return "Severe"

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
AZURE_CONNECTION_STRING = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()

if not SUPABASE_URL or not SERVICE_KEY:
    raise ValueError(
        "Could not find SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in sipat-ml/.env"
    )


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


def _friendly_error_message(exc: Exception) -> str:
    if isinstance(exc, APIError) and exc.args:
        payload = exc.args[0]
        if isinstance(payload, dict):
            message = payload.get("message") or str(exc)
            details = payload.get("details")
            hint = payload.get("hint")
            code = payload.get("code")
            parts = [str(message)]
            if hint:
                parts.append(f"hint={hint}")
            if code:
                parts.append(f"code={code}")
            if details:
                parts.append(f"details={details}")
            return " | ".join(parts)

    return str(exc)


def _build_supabase_client() -> Client:
    _validate_supabase_config(SUPABASE_URL, SERVICE_KEY)
    return create_client(SUPABASE_URL, SERVICE_KEY)


def _claim_oldest_queued_ride(supabase: Client) -> dict[str, Any] | None:
    response = (
        supabase.table("rides_metadata")
        .select("*")
        .eq("status", "queued")
        .order("created_at", desc=False, nullsfirst=True)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows:
        return None

    ride = rows[0]
    ride_id = ride.get("id")
    if not ride_id:
        raise KeyError("Claimed ride row does not include an id column")

    supabase.table("rides_metadata").update({"status": "processing"}).eq(
        "id", ride_id
    ).execute()
    ride["status"] = "processing"
    return ride


def _mark_failed(supabase: Client, ride_id: str, error_message: str) -> None:
    supabase.table("rides_metadata").update(
        {"status": "failed", "error_log": error_message}
    ).eq("id", ride_id).execute()


def _first_present_value(row: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_video_path(row: dict[str, Any]) -> str:
    video_path = _first_present_value(
        row,
        [
            "video_bucket_path",
            "video_path",
            "video_uri",
            "video_file_path",
            "video_file_uri",
            "video_object_path",
            "videoName",
            "video_name",
            "file_name",
            "filename",
        ],
    )
    if video_path:
        return video_path

    raise KeyError("Could not determine the video path from rides_metadata row")


def _resolve_gps_path(row: dict[str, Any], video_path: str) -> str:
    gps_path = _first_present_value(
        row,
        [
            "gps_bucket_path",
            "gps_json_path",
            "gps_path",
            "gps_uri",
            "gps_file_path",
            "gps_file_uri",
            "gps_object_path",
            "gps_log_path",
            "csv_path",
            "csv_uri",
            "metadata_path",
        ],
    )
    if gps_path:
        if gps_path.lower().endswith(".csv"):
            return str(Path(gps_path).with_suffix(".json"))
        return gps_path

    return str(Path(video_path).with_suffix(".json"))


_blob_service: BlobServiceClient | None = None


def _get_blob_service() -> BlobServiceClient:
    global _blob_service
    if _blob_service is None:
        if not AZURE_CONNECTION_STRING:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set in environment")
        _blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    return _blob_service


def _download_object_to_temp_folder(
    bucket_name: str,
    object_path: str,
    temp_dir: Path,
    supabase: Client,
) -> Path:
    blob_service = _get_blob_service()
    blob_client = blob_service.get_blob_client(container=bucket_name, blob=object_path)
    download_stream = blob_client.download_blob(timeout=120)
    file_bytes = download_stream.readall()
    local_path = temp_dir / Path(object_path).name
    local_path.write_bytes(file_bytes)
    return local_path


def _load_yolo_model() -> YOLO:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"YOLO weights not found at: {MODEL_PATH}")
    return YOLO(str(MODEL_PATH))


def _repair_video(video_path: Path) -> Path:
    repaired_path = video_path.with_stem(video_path.stem + "_repaired")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-c", "copy",
                "-movflags", "+faststart",
                str(repaired_path),
            ],
            capture_output=True,
            check=True,
        )
        return repaired_path
    except (FileNotFoundError, subprocess.CalledProcessError):
        return video_path


def _insert_raw_detections(supabase: Client, raw_batch: list[dict[str, Any]]) -> None:
    if not raw_batch:
        print("No raw detections were generated for this ride")
        return

    print(f"Uploading {len(raw_batch)} raw frame detections to public.raw_detections...")
    supabase.schema("public").from_("raw_detections").insert(raw_batch).execute()


def _fetch_verified_potholes(supabase: Client) -> list[dict[str, Any]]:
    response = (
        supabase.schema("public")
        .from_("verified_potholes")
        .select("id, consolidated_latitude, consolidated_longitude, worst_severity, total_detection_hits, status, updated_at, user_detections")
        .execute()
    )
    return response.data or []


def _find_matching_verified_pothole(
    existing_potholes: list[dict[str, Any]],
    target_lat: float,
    target_lng: float,
    merge_radius_meters: float = MERGE_RADIUS_METERS,
) -> dict[str, Any] | None:
    closest_match: dict[str, Any] | None = None
    closest_distance = merge_radius_meters

    for pothole in existing_potholes:
        lat = pothole.get("consolidated_latitude")
        lng = pothole.get("consolidated_longitude")
        if lat is None or lng is None:
            continue

        distance_meters = haversine_distance_meters(
            float(lat), float(lng), target_lat, target_lng
        )
        if distance_meters <= closest_distance:
            closest_distance = distance_meters
            closest_match = pothole

    return closest_match


def _merge_user_detections(
    existing_list: list[dict[str, Any]],
    incoming_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    best: dict[str, float] = {}
    for entry in existing_list:
        uid = entry.get("user_id")
        ts = entry.get("video_timestamp")
        if uid and ts is not None:
            if uid not in best or ts < best[uid]:
                best[uid] = ts
    for entry in incoming_list:
        uid = entry.get("user_id")
        ts = entry.get("video_timestamp")
        if uid and ts is not None:
            if uid not in best or ts < best[uid]:
                best[uid] = ts
    return sorted(
        [{"user_id": uid, "video_timestamp": ts} for uid, ts in best.items()],
        key=lambda x: x["video_timestamp"],
    )


def _sync_verified_potholes(
    supabase: Client,
    raw_batch: list[dict[str, Any]],
    ride_id: str,
) -> int:
    clustered_potholes = cluster_pothole_detections(
        raw_batch,
        max_distance_meters=MERGE_RADIUS_METERS,
        min_detections=3,
    )

    if not clustered_potholes:
        print("No stable pothole clusters were produced from this ride")
        return 0

    existing_potholes = _fetch_verified_potholes(supabase)
    touched_potholes = 0

    for pothole in clustered_potholes:
        lat = float(pothole["lat"])
        lng = float(pothole["lng"])
        new_hits = int(pothole.get("detection_count") or 0)
        max_area_m2 = pothole.get("max_area_m2")
        ipm_severity = _phys_area_to_severity(max_area_m2)
        frame_severity = pothole.get("max_frame_severity", "Minor")
        avg_confidence = pothole.get("avg_confidence", 0.0)
        severity_order = {"Minor": 0, "Moderate": 1, "Severe": 2}
        new_severity = frame_severity if severity_order.get(ipm_severity, 0) > severity_order.get(frame_severity, 0) else ipm_severity
        if avg_confidence < CONFIDENCE_MODERATE_CAP:
            new_severity = "Minor"
        elif avg_confidence < CONFIDENCE_SEVERE_CAP and severity_order.get(new_severity, 0) > 1:
            new_severity = "Moderate"
        print(f"  severity: ipm={ipm_severity} (area={max_area_m2}), frame={frame_severity}, "
              f"conf={avg_confidence:.3f} → final={new_severity}")
        matched_pothole = _find_matching_verified_pothole(
            existing_potholes,
            lat,
            lng,
        )

        if matched_pothole:
            current_hits = int(matched_pothole.get("total_detection_hits") or 0)
            updated_hits = current_hits + new_hits
            current_severity = matched_pothole.get("worst_severity") or "Minor"
            updated_severity = new_severity if severity_order.get(new_severity, 0) >= severity_order.get(current_severity, 0) else current_severity
            existing_user_detections = (
                matched_pothole.get("user_detections")
                if isinstance(matched_pothole.get("user_detections"), list)
                else []
            )
            merged_user_detections = _merge_user_detections(
                existing_user_detections,
                pothole.get("user_detections") or [],
            )
            update_payload = {
                "total_detection_hits": updated_hits,
                "worst_severity": updated_severity,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user_detections": merged_user_detections,
            }
            supabase.schema("public").from_("verified_potholes").update(
                update_payload
            ).eq("id", matched_pothole["id"]).execute()
            matched_pothole["total_detection_hits"] = updated_hits
            matched_pothole["worst_severity"] = updated_severity
            matched_pothole["updated_at"] = datetime.now(timezone.utc).isoformat()
            matched_pothole["user_detections"] = merged_user_detections
            touched_potholes += 1
            continue

        total_hits = new_hits
        severity = new_severity
        new_user_detections = pothole.get("user_detections") or []
        insert_payload = {
            "ride_id": ride_id,
            "consolidated_latitude": lat,
            "consolidated_longitude": lng,
            "worst_severity": severity,
            "total_detection_hits": total_hits,
            "status": "queued",
            "user_detections": new_user_detections,
        }
        supabase.schema("public").from_("verified_potholes").insert(
            insert_payload
        ).execute()
        existing_potholes.append(
            {
                "id": None,
                "consolidated_latitude": lat,
                "consolidated_longitude": lng,
                "worst_severity": severity,
                "total_detection_hits": total_hits,
                "status": "queued",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user_detections": new_user_detections,
            }
        )
        touched_potholes += 1

    print(f"Synced {touched_potholes} verified pothole records for ride {ride_id}")
    return touched_potholes


def _mark_completed(supabase: Client, ride_id: str) -> None:
    supabase.table("rides_metadata").update({"status": "completed"}).eq(
        "id", ride_id
    ).execute()


def _process_ride(supabase: Client, ride: dict[str, Any]) -> dict[str, Any]:
    ride_id = str(ride.get("id") or "")
    if not ride_id:
        raise KeyError("Ride row does not include an id column")

    video_path = _resolve_video_path(ride)
    gps_path = _resolve_gps_path(ride, video_path)
    user_id = ride.get("user_id")

    with tempfile.TemporaryDirectory(prefix=f"ride_{ride_id}_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        video_local_path = _download_object_to_temp_folder(
            "raw-road-data", video_path, temp_dir, supabase
        )
        video_local_path = _repair_video(video_local_path)
        gps_local_path = _download_object_to_temp_folder(
            "raw-road-data", gps_path, temp_dir, supabase
        )

        model = _load_yolo_model()
        gps_processor = GPSProcessor.from_json_file(gps_local_path)
        builder = DetectionBatchBuilder(
            ride_id=ride_id,
            user_id=str(user_id) if user_id is not None else None,
            supabase=supabase,
            model=model,
            supabase_url=SUPABASE_URL,
        )
        raw_batch = builder.build(video_local_path, gps_processor)
        _insert_raw_detections(supabase, raw_batch)
        _sync_verified_potholes(supabase, raw_batch, ride_id)
        _mark_completed(supabase, ride_id)

        print(f"Locked ride {ride_id} for processing")
        print(f"Downloaded video: {video_local_path}")
        print(f"Downloaded GPS log: {gps_local_path}")
        print(f"Generated {len(raw_batch)} raw detections")
        print(f"Marked ride {ride_id} as completed")

        return {
            "ride_id": ride_id,
            "video_path": str(video_local_path),
            "gps_path": str(gps_local_path),
            "raw_detection_count": len(raw_batch),
            "source_video_object": video_path,
            "source_gps_object": gps_path,
        }


def process_next_queued_ride() -> dict[str, Any] | None:
    supabase = _build_supabase_client()
    print("Connected to Supabase with service role key")

    ride = _claim_oldest_queued_ride(supabase)
    if ride is None:
        print("No queued rides found in rides_metadata")
        return None

    ride_id = str(ride.get("id") or "")
    if not ride_id:
        raise KeyError("Claimed ride row does not include an id column")

    try:
        return _process_ride(supabase, ride)
    except (APIError, StorageException, OSError, ValueError, KeyError) as exc:
        error_message = _friendly_error_message(exc)
        traceback_text = traceback.format_exc()
        combined_error = f"{error_message}\n{traceback_text}"
        try:
            _mark_failed(supabase, ride_id, combined_error)
        except Exception as mark_exc:
            print(f"Failed to mark ride {ride_id} as failed: {mark_exc}")
        raise RuntimeError(combined_error) from exc


def process_ride_by_id(ride_id: str) -> dict[str, Any]:
    supabase = _build_supabase_client()
    response = supabase.table("rides_metadata").select("*").eq("id", ride_id).execute()
    rows = response.data or []
    if not rows:
        raise KeyError(f"Ride {ride_id} not found")

    ride = rows[0]
    supabase.table("rides_metadata").update({"status": "processing"}).eq(
        "id", ride_id
    ).execute()
    ride["status"] = "processing"

    try:
        return _process_ride(supabase, ride)
    except (APIError, StorageException, OSError, ValueError, KeyError) as exc:
        error_message = _friendly_error_message(exc)
        traceback_text = traceback.format_exc()
        combined_error = f"{error_message}\n{traceback_text}"
        try:
            _mark_failed(supabase, ride_id, combined_error)
        except Exception as mark_exc:
            print(f"Failed to mark ride {ride_id} as failed: {mark_exc}")
        raise RuntimeError(combined_error) from exc


if __name__ == "__main__":
    while True:
        try:
            result = process_next_queued_ride()
            if result is None:
                print("No queued rides found. Sleeping for 10 minutes...")
            else:
                print(
                    f"Finished ride {result['ride_id']} with {result['raw_detection_count']} raw detections. Sleeping for 10 minutes..."
                )
        except Exception as exc:
            print(f"Batch worker cycle failed: {exc}")
        time.sleep(600)
