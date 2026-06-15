import base64
import json
import os
import re
from urllib.parse import urlparse

import cv2
from clustering import cluster_pothole_detections
from dotenv import load_dotenv
from postgrest.exceptions import APIError
from supabase import Client, create_client
from ultralytics import YOLO
from utils.geo_sync import interpolate_coordinate_at_time

# 1. Dynamically locate and load the .env file at the root level
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "..", ".env")
load_dotenv(dotenv_path=env_path)

# 2. Pull the variables securely from the environment
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

if not SUPABASE_URL or not SERVICE_KEY:
    raise ValueError("❌ Error: Could not find SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in your .env file!")


def _supabase_project_ref_from_url(url: str) -> str | None:
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return None

    # Typical hosted Supabase URL: https://<project-ref>.supabase.co
    if hostname.endswith(".supabase.co"):
        return hostname.split(".")[0]
    return None


def _jwt_payload(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None


def _validate_supabase_config(url: str, key: str) -> None:
    # Supabase keys are JWTs (three dot-separated segments). Fail fast if malformed.
    if key.count(".") < 2:
        raise ValueError(
            "❌ SUPABASE_SERVICE_ROLE_KEY doesn't look like a Supabase JWT (expected 3 dot-separated segments)."
        )

    url_ref = _supabase_project_ref_from_url(url)
    payload = _jwt_payload(key) or {}
    key_ref = payload.get("ref") if isinstance(payload.get("ref"), str) else None
    role = payload.get("role") if isinstance(payload.get("role"), str) else None

    if role and role != "service_role":
        raise ValueError(
            f"❌ SUPABASE_SERVICE_ROLE_KEY is not a service_role token (role={role!r}). "
            "Paste the service_role key from Supabase Dashboard → Project Settings → API."
        )

    # If we can infer both refs, ensure URL and key are for the same project.
    if url_ref and key_ref and url_ref != key_ref:
        raise ValueError(
            "❌ Supabase project mismatch: your SUPABASE_URL points to a different project than your SUPABASE_SERVICE_ROLE_KEY.\n"
            f"   - URL project ref: {url_ref}\n"
            f"   - Key project ref: {key_ref}\n"
            "Fix: Open Supabase Dashboard → Project Settings → API for the URL's project, then copy its service_role key into sipat-ml/.env."
        )


def _extract_json_from_details(details: object) -> dict | None:
    if details is None:
        return None
    if isinstance(details, (bytes, bytearray)):
        text = details.decode("utf-8", "ignore")
    else:
        text = str(details)

    # postgrest sometimes packs a JSON string inside a python-bytes repr: b'{"message":...}'
    match = re.search(r"(\{.*\})", text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _raise_friendly_postgrest_error(action: str, error: APIError) -> None:
    payload = error.args[0] if error.args else None
    if not isinstance(payload, dict):
        raise RuntimeError(f"Supabase request failed while {action}: {error}") from error

    code = payload.get("code")
    message = payload.get("message")
    hint = payload.get("hint")
    details = payload.get("details")

    inner = _extract_json_from_details(details)
    if isinstance(inner, dict):
        message = inner.get("message") or message
        hint = inner.get("hint") or hint

    summary = f"Supabase request failed while {action}."
    if message:
        summary += f" Message: {message}"
    if hint:
        summary += f" Hint: {hint}"
    if code:
        summary += f" (code={code})"

    # Add the most common fix for this project.
    if str(code) == "401" or (isinstance(message, str) and "Invalid API key" in message):
        summary += "\nFix: Ensure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY come from the SAME Supabase project."

    raise RuntimeError(summary) from error

# 3. Initialize Supabase
_validate_supabase_config(SUPABASE_URL, SERVICE_KEY)
supabase: Client = create_client(SUPABASE_URL, SERVICE_KEY)
print("🔑 Supabase client initialized using service role key")

# 4. Load your custom-trained YOLO26 model
model = YOLO("../weights/best.pt")


def run_processing_pipeline(video_file_path, gps_json_path, ride_id, user_id):
    # Safety check for required mock input files
    if not os.path.exists(gps_json_path):
        print(f"❌ Error: Cannot find your mock GPS file at: {gps_json_path}")
        print("Please ensure you created 'mock_gps.json' inside your 'sipat-ml' root folder.")
        return

    if not os.path.exists(video_file_path):
        print(f"⚠️ Note: '{video_file_path}' not found. Drop a test video file into your root folder to process raw frames.")
        print("💡 Skipping frame analysis loop and running a dry test directly on mock metrics...")
        # Fallback to run a dry-test execution using manual data points if no video file exists yet
        mock_raw_batch = [
            {"ride_id": ride_id, "user_id": user_id, "lat": 14.554811, "lng": 121.048102, "video_timestamp": 0.5},
            {"ride_id": ride_id, "user_id": user_id, "lat": 14.554813, "lng": 121.048104, "video_timestamp": 0.8},
        ]
        process_and_upload_results(mock_raw_batch, ride_id)
        return

    # Open the video stream
    cap = cv2.VideoCapture(video_file_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0:
        fps = 30.0 # Fallback default

    with open(gps_json_path) as f:
        gps_data = json.load(f)

    raw_detections_batch = []
    print("🎬 Processing video frames with YOLO26...")

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break

        frame_id = cap.get(cv2.CAP_PROP_POS_FRAMES)
        timestamp_seconds = frame_id / fps

        # Execute YOLO26 detection
        results = model(frame, conf=0.4, verbose=False)

        for result in results:
            if len(result.boxes) > 0:
                lat, lng = interpolate_coordinate_at_time(gps_data, timestamp_seconds)
                raw_detections_batch.append({
                    "ride_id": ride_id,
                    "user_id": user_id,
                    "lat": lat,
                    "lng": lng,
                    "video_timestamp": timestamp_seconds
                })

    cap.release()
    process_and_upload_results(raw_detections_batch, ride_id)


def process_and_upload_results(raw_batch, ride_id):
    if not raw_batch:
        print("☀️ No potholes found in video sequence!")
        return

    # Bulk insert raw data points into Supabase
    print(f"📡 Uploading {len(raw_batch)} raw frame detections to Supabase database...")
    try:
        supabase.table("raw_detections").insert(raw_batch).execute()
    except APIError as e:
        _raise_friendly_postgrest_error("inserting into raw_detections", e)

    # Run your custom spatial DBSCAN clustering
    print("🧠 Running DBSCAN Spatial Clustering (3-meter radius validation)...")
    clean_pins = cluster_pothole_detections(raw_batch, max_distance_meters=3.0, min_detections=3)

    # Insert deduplicated map markers into verified_potholes
    if clean_pins:
        for pin in clean_pins:
            pin["ride_id"] = ride_id
            try:
                supabase.table("verified_potholes").insert(pin).execute()
            except APIError as e:
                _raise_friendly_postgrest_error("inserting into verified_potholes", e)
        print(f"🚀 Success! Generated {len(clean_pins)} unique map pins inside 'verified_potholes' table.")


# ========================================================
# AUTOMATED RUNNER
# ========================================================
if __name__ == "__main__":
    # Point paths upward since our terminal is open inside the /processing directory
    run_processing_pipeline(
        video_file_path="../sample_road_video.mp4",
        gps_json_path="../mock_gps.json",
        ride_id="a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",  # Standard mock UUID format
        user_id=None                                     # Optional auth link
    )
