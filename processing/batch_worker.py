import base64
import subprocess
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
import time
import tempfile
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from postgrest.exceptions import APIError
from ultralytics import YOLO
from storage3.utils import StorageException
from supabase import Client, create_client

from clustering import cluster_pothole_detections

CURRENT_DIR = Path(__file__).resolve().parent
ENV_PATH = CURRENT_DIR.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

MODEL_PATH = CURRENT_DIR.parent / "weights" / "best.pt"
EARTH_RADIUS_METERS = 6371008.8
MERGE_RADIUS_METERS = 3.0
DEFAULT_PIXELS_PER_METER = 100.0
DEFAULT_ROAD_WIDTH_METERS = 6.0
DEFAULT_LOOKAHEAD_METERS = 30.0

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


@dataclass(frozen=True)
class GPSIndex:
    timestamps: list[float]
    latlng: list[tuple[float, float]]
    headings: list[float | None]


@dataclass(frozen=True)
class IPMContext:
    matrix: np.ndarray
    pixels_per_meter: float
    output_width_px: int
    output_height_px: int
    frame_width: int
    frame_height: int


def _parse_heading(item: dict[str, Any]) -> float | None:
    for key in ("vehicle_heading_degrees", "heading_degrees", "heading"):
        if key not in item:
            continue
        value = item.get(key)
        if value is None:
            return None
        try:
            return float(value) % 360.0
        except (TypeError, ValueError):
            return None
    return None


def _build_gps_index(gps_data: list[dict[str, Any]]) -> GPSIndex:
    samples: list[tuple[float, float, float, float | None]] = []
    for item in gps_data:
        try:
            timestamp = float(item["timestamp_seconds"])
            lat = float(item["lat"])
            lng = float(item["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        heading = _parse_heading(item)
        samples.append((timestamp, lat, lng, heading))

    if not samples:
        raise ValueError("GPS JSON must include timestamp_seconds, lat, and lng values")

    samples.sort(key=lambda row: row[0])
    return GPSIndex(
        timestamps=[row[0] for row in samples],
        latlng=[(row[1], row[2]) for row in samples],
        headings=[row[3] for row in samples],
    )


def _closest_gps_index(gps_index: GPSIndex, current_time: float) -> int:
    pos = bisect_left(gps_index.timestamps, current_time)
    if pos <= 0:
        return 0
    if pos >= len(gps_index.timestamps):
        return len(gps_index.timestamps) - 1
    before = pos - 1
    after = pos
    if abs(gps_index.timestamps[before] - current_time) <= abs(
        gps_index.timestamps[after] - current_time
    ):
        return before
    return after


def _lerp_heading_degrees(
    start_deg: float, end_deg: float, fraction: float
) -> float:
    fraction = max(0.0, min(1.0, fraction))
    delta = (end_deg - start_deg + 180.0) % 360.0 - 180.0
    return (start_deg + fraction * delta + 360.0) % 360.0


def _interpolated_gps_sample(
    gps_index: GPSIndex, current_time: float
) -> tuple[int, float, float, float | None]:
    timestamps = gps_index.timestamps
    pos = bisect_left(timestamps, current_time)
    if pos <= 0:
        lat, lng = gps_index.latlng[0]
        return 0, lat, lng, gps_index.headings[0]
    if pos >= len(timestamps):
        last = len(timestamps) - 1
        lat, lng = gps_index.latlng[last]
        return last, lat, lng, gps_index.headings[last]

    t1 = timestamps[pos - 1]
    t2 = timestamps[pos]
    lat1, lng1 = gps_index.latlng[pos - 1]
    lat2, lng2 = gps_index.latlng[pos]
    if t2 == t1:
        heading = gps_index.headings[pos - 1]
        return pos - 1, lat1, lng1, heading

    fraction = (current_time - t1) / (t2 - t1)
    lat = lat1 + fraction * (lat2 - lat1)
    lng = lng1 + fraction * (lng2 - lng1)

    heading1 = gps_index.headings[pos - 1]
    heading2 = gps_index.headings[pos]
    heading = None
    if heading1 is not None and heading2 is not None:
        heading = _lerp_heading_degrees(heading1, heading2, fraction)

    if abs(current_time - t1) <= abs(t2 - current_time):
        idx = pos - 1
    else:
        idx = pos

    return idx, lat, lng, heading


def _bearing_degrees(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    lat_a_rad = math.radians(lat_a)
    lat_b_rad = math.radians(lat_b)
    delta_lng = math.radians(lng_b - lng_a)

    x = math.sin(delta_lng) * math.cos(lat_b_rad)
    y = math.cos(lat_a_rad) * math.sin(lat_b_rad) - math.sin(lat_a_rad) * math.cos(
        lat_b_rad
    ) * math.cos(delta_lng)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _estimate_heading_degrees(gps_index: GPSIndex, idx: int) -> float | None:
    sample_count = len(gps_index.latlng)
    if sample_count < 2:
        return None

    if idx <= 0:
        start_idx, end_idx = 0, 1
    elif idx >= sample_count - 1:
        start_idx, end_idx = sample_count - 2, sample_count - 1
    else:
        start_idx, end_idx = idx - 1, idx + 1

    lat_a, lng_a = gps_index.latlng[start_idx]
    lat_b, lng_b = gps_index.latlng[end_idx]
    if lat_a == lat_b and lng_a == lng_b:
        return None

    return _bearing_degrees(lat_a, lng_a, lat_b, lng_b)


def _offset_lat_lng(
    base_lat: float,
    base_lng: float,
    dx_meters: float,
    dy_meters: float,
    bearing_degrees: float,
) -> tuple[float, float]:
    bearing_rad = math.radians(bearing_degrees)
    north_m = dy_meters * math.cos(bearing_rad) - dx_meters * math.sin(bearing_rad)
    east_m = dy_meters * math.sin(bearing_rad) + dx_meters * math.cos(bearing_rad)

    lat_rad = math.radians(base_lat)
    cos_lat = math.cos(lat_rad)
    if abs(cos_lat) < 1e-12:
        return base_lat, base_lng

    delta_lat = north_m / EARTH_RADIUS_METERS
    delta_lng = east_m / (EARTH_RADIUS_METERS * cos_lat)

    return (
        base_lat + math.degrees(delta_lat),
        base_lng + math.degrees(delta_lng),
    )


def _default_roi_points(frame_width: int, frame_height: int) -> np.ndarray:
    center_x = frame_width * 0.5
    top_y = frame_height * 0.6
    bottom_y = frame_height * 0.95
    top_half_width = frame_width * 0.1
    bottom_half_width = frame_width * 0.45

    return np.float32(
        [
            [center_x - top_half_width, top_y],
            [center_x + top_half_width, top_y],
            [center_x + bottom_half_width, bottom_y],
            [center_x - bottom_half_width, bottom_y],
        ]
    )


def _build_ipm_context(
    frame_width: int,
    frame_height: int,
    pixels_per_meter: float = DEFAULT_PIXELS_PER_METER,
    road_width_meters: float = DEFAULT_ROAD_WIDTH_METERS,
    lookahead_meters: float = DEFAULT_LOOKAHEAD_METERS,
) -> IPMContext:
    src = _default_roi_points(frame_width, frame_height)
    output_width_px = max(1, int(road_width_meters * pixels_per_meter))
    output_height_px = max(1, int(lookahead_meters * pixels_per_meter))
    dst = np.float32(
        [
            [0.0, 0.0],
            [float(output_width_px), 0.0],
            [float(output_width_px), float(output_height_px)],
            [0.0, float(output_height_px)],
        ]
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    return IPMContext(
        matrix=matrix,
        pixels_per_meter=pixels_per_meter,
        output_width_px=output_width_px,
        output_height_px=output_height_px,
        frame_width=frame_width,
        frame_height=frame_height,
    )


def _bottom_center_point(box: Any) -> tuple[float, float] | None:
    xyxy = getattr(box, "xyxy", None)
    if xyxy is None:
        return None
    try:
        coords = xyxy[0].tolist()
    except Exception:
        try:
            coords = list(xyxy[0])
        except Exception:
            return None
    if len(coords) < 4:
        return None
    x1, _, x2, y2 = map(float, coords[:4])
    return (x1 + x2) * 0.5, y2


def _ipm_pixel_to_offset(
    pixel_point: tuple[float, float] | None,
    ipm_context: IPMContext | None,
) -> tuple[float, float]:
    if ipm_context is None or pixel_point is None:
        return 0.0, 0.0

    x, y = pixel_point
    x = float(np.clip(x, 0.0, ipm_context.frame_width - 1))
    y = float(np.clip(y, 0.0, ipm_context.frame_height - 1))

    point = np.array([[[x, y]]], dtype=np.float32)
    transformed = cv2.perspectiveTransform(point, ipm_context.matrix)
    x_bev, y_bev = transformed[0][0]
    if not np.isfinite(x_bev) or not np.isfinite(y_bev):
        return 0.0, 0.0

    dx_meters = (float(x_bev) - (ipm_context.output_width_px / 2.0)) / ipm_context.pixels_per_meter
    dy_meters = (ipm_context.output_height_px - float(y_bev)) / ipm_context.pixels_per_meter
    return dx_meters, dy_meters


def _project_detection_to_gps(
    gps_index: GPSIndex,
    current_time: float,
    dx_meters: float,
    dy_meters: float,
) -> tuple[float, float]:
    idx, base_lat, base_lng, heading = _interpolated_gps_sample(
        gps_index, current_time
    )
    if dx_meters == 0.0 and dy_meters == 0.0:
        return base_lat, base_lng

    if heading is None:
        heading = _estimate_heading_degrees(gps_index, idx)
    if heading is None:
        return base_lat, base_lng

    return _offset_lat_lng(base_lat, base_lng, dx_meters, dy_meters, heading)


def _load_gps_data(gps_json_path: Path) -> list[dict[str, Any]]:
    with gps_json_path.open("r", encoding="utf-8") as gps_file:
        gps_data = json.load(gps_file)

    if not isinstance(gps_data, list):
        raise ValueError("GPS JSON must contain a list of timestamped samples")

    return gps_data


def _build_raw_detection_batch(
    video_path: Path,
    gps_path: Path,
    ride_id: str,
    user_id: str | None,
) -> list[dict[str, Any]]:
    model = _load_yolo_model()
    gps_data = _load_gps_data(gps_path)
    gps_index = _build_gps_index(gps_data)

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open downloaded video: {video_path}")

    try:
        frames_per_second = capture.get(cv2.CAP_PROP_FPS)
        if not frames_per_second or frames_per_second <= 0:
            frames_per_second = 30.0

        raw_detections_batch: list[dict[str, Any]] = []
        frame_index = 0
        ipm_context: IPMContext | None = None

        while capture.isOpened():
            success, frame = capture.read()
            if not success:
                break

            timestamp_seconds = frame_index / frames_per_second
            if ipm_context is None:
                ipm_context = _build_ipm_context(frame.shape[1], frame.shape[0])
            results = model(frame, conf=0.4, verbose=False)

            for result in results:
                if not getattr(result, "boxes", None):
                    continue

                for _box in result.boxes:
                    bottom_center = _bottom_center_point(_box)
                    dx_meters, dy_meters = _ipm_pixel_to_offset(bottom_center, ipm_context)
                    lat, lng = _project_detection_to_gps(
                        gps_index,
                        timestamp_seconds,
                        dx_meters,
                        dy_meters,
                    )
                    raw_detections_batch.append(
                        {
                            "ride_id": ride_id,
                            "user_id": user_id,
                            "lat": lat,
                            "lng": lng,
                            "video_timestamp": round(timestamp_seconds, 2),
                        }
                    )

            frame_index += 1

        return raw_detections_batch
    finally:
        capture.release()


def _insert_raw_detections(supabase: Client, raw_batch: list[dict[str, Any]]) -> None:
    if not raw_batch:
        print("No raw detections were generated for this ride")
        return

    print(f"Uploading {len(raw_batch)} raw frame detections to public.raw_detections...")
    supabase.schema("public").from_("raw_detections").insert(raw_batch).execute()


def _haversine_distance_meters(
    lat_a: float,
    lng_a: float,
    lat_b: float,
    lng_b: float,
) -> float:
    lat_a_rad, lng_a_rad, lat_b_rad, lng_b_rad = np.radians(
        [lat_a, lng_a, lat_b, lng_b]
    )
    delta_lat = lat_b_rad - lat_a_rad
    delta_lng = lng_b_rad - lng_a_rad

    a = (
        np.sin(delta_lat / 2.0) ** 2
        + np.cos(lat_a_rad) * np.cos(lat_b_rad) * np.sin(delta_lng / 2.0) ** 2
    )
    return float(2.0 * EARTH_RADIUS_METERS * np.arcsin(np.sqrt(a)))


def _fetch_verified_potholes(supabase: Client) -> list[dict[str, Any]]:
    response = (
        supabase.schema("public")
        .from_("verified_potholes")
        .select("id, lat, lng, detection_count, status, updated_at")
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
        lat = pothole.get("lat")
        lng = pothole.get("lng")
        if lat is None or lng is None:
            continue

        distance_meters = _haversine_distance_meters(
            float(lat), float(lng), target_lat, target_lng
        )
        if distance_meters <= closest_distance:
            closest_distance = distance_meters
            closest_match = pothole

    return closest_match


def _sync_verified_potholes(
    supabase: Client,
    raw_batch: list[dict[str, Any]],
    ride_id: str,
) -> int:
    clustered_potholes = cluster_pothole_detections(
        raw_batch,
        max_distance_meters=MERGE_RADIUS_METERS,
        min_detections=2,
    )

    if not clustered_potholes:
        print("No stable pothole clusters were produced from this ride")
        return 0

    existing_potholes = _fetch_verified_potholes(supabase)
    touched_potholes = 0

    for pothole in clustered_potholes:
        lat = float(pothole["lat"])
        lng = float(pothole["lng"])
        detection_count = int(pothole.get("detection_count") or 0)
        matched_pothole = _find_matching_verified_pothole(
            existing_potholes,
            lat,
            lng,
        )

        if matched_pothole:
            current_count = int(matched_pothole.get("detection_count") or 0)
            updated_count = current_count + detection_count
            supabase.schema("public").from_("verified_potholes").update(
                {
                    "detection_count": updated_count,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", matched_pothole["id"]).execute()
            matched_pothole["detection_count"] = updated_count
            matched_pothole["updated_at"] = datetime.now(timezone.utc).isoformat()
            touched_potholes += 1
            continue

        supabase.schema("public").from_("verified_potholes").insert(
            {
                "ride_id": ride_id,
                "lat": lat,
                "lng": lng,
                "detection_count": detection_count,
                "status": "queued",
            }
        ).execute()
        existing_potholes.append(
            {
                "id": None,
                "lat": lat,
                "lng": lng,
                "detection_count": detection_count,
                "status": "queued",
                "updated_at": datetime.now(timezone.utc).isoformat(),
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

        raw_batch = _build_raw_detection_batch(
            video_path=video_local_path,
            gps_path=gps_local_path,
            ride_id=ride_id,
            user_id=str(user_id) if user_id is not None else None,
        )
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
