"""Reprocess GPS coordinates for existing rides.

The original client stored GPS timestamps as Date.now() epoch milliseconds,
but the backend expected relative seconds from recording start. This caused
all detections to be mapped to the ride's starting GPS coordinate.

This script:
1. Downloads each ride's GPS JSON from blob storage
2. Normalizes timestamps from epoch ms to relative seconds
3. Re-interpolates GPS coordinates for each raw_detection
4. Updates raw_detections.lat and raw_detections.lng
5. Re-clusters into verified_potholes with corrected centroids

Usage:
    cd sipat-ml
    python -m processing.scripts.reprocess_gps_coords [--dry-run] [--limit N] [--ride-id ID]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import tempfile
import time
from bisect import bisect_left
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

load_dotenv()

from processing.config.settings import RAW_DATA_BUCKET, MERGE_RADIUS_METERS, CLUSTER_MIN_DETECTIONS, EARTH_RADIUS_METERS
from processing.services.blob_storage import BlobStorageService, get_blob_storage_service
from processing.services.supabase_client import SupabaseService, get_supabase_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---- GPS normalization ----

def normalize_gps_timestamps(gps_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert epoch millisecond timestamps to relative seconds from first sample."""
    if not gps_data:
        return gps_data

    first_ts = None
    for item in gps_data:
        ts = item.get("timestamp_seconds")
        if ts is not None:
            try:
                first_ts = float(ts)
                break
            except (TypeError, ValueError):
                continue

    if first_ts is None:
        return gps_data

    normalized = []
    for item in gps_data:
        ts = item.get("timestamp_seconds")
        if ts is not None:
            try:
                new_ts = (float(ts) - first_ts) / 1000.0
            except (TypeError, ValueError):
                new_ts = 0.0
        else:
            new_ts = 0.0
        normalized.append({**item, "timestamp_seconds": new_ts})

    return normalized


def is_already_normalized(gps_data: list[dict[str, Any]]) -> bool:
    """Check if timestamps are already in relative seconds (0-10000 range)."""
    for item in gps_data[:5]:
        ts = item.get("timestamp_seconds")
        if ts is not None:
            try:
                if float(ts) > 1_000_000:  # epoch ms is > 1 trillion
                    return False
            except (TypeError, ValueError):
                continue
    return True


# ---- GPS interpolation (mirrors gps_processor.py logic) ----

def interpolate_sample(
    gps_data: list[dict[str, Any]],
    current_time: float,
) -> tuple[float, float] | None:
    """Interpolate GPS lat/lng at a given time (relative seconds)."""
    samples = []
    for item in gps_data:
        try:
            ts = float(item["timestamp_seconds"])
            lat = float(item["lat"])
            lng = float(item["lng"])
            samples.append((ts, lat, lng))
        except (KeyError, TypeError, ValueError):
            continue

    if not samples:
        return None

    samples.sort(key=lambda row: row[0])
    timestamps = [s[0] for s in samples]

    pos = bisect_left(timestamps, current_time)
    if pos <= 0:
        return samples[0][1], samples[0][2]
    if pos >= len(samples):
        return samples[-1][1], samples[-1][2]

    t1, lat1, lng1 = samples[pos - 1]
    t2, lat2, lng2 = samples[pos]
    if t2 == t1:
        return lat1, lng1

    fraction = (current_time - t1) / (t2 - t1)
    lat = lat1 + fraction * (lat2 - lat1)
    lng = lng1 + fraction * (lng2 - lng1)
    return lat, lng


# ---- main logic ----

def fetch_completed_rides(svc: SupabaseService, limit: int | None, ride_id: str | None) -> list[dict]:
    query = svc.client.table("rides_metadata").select("*").eq("status", "completed")
    if ride_id:
        query = query.eq("id", ride_id)
    query = query.order("created_at", desc=False)
    if limit:
        query = query.limit(limit)
    return (query.execute().data) or []


def resolve_gps_path(ride: dict) -> str:
    for key in ["gps_bucket_path", "gps_json_path", "gps_path"]:
        val = ride.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    video = None
    for key in ["video_bucket_path", "video_path"]:
        video = ride.get(key)
        if video:
            break
    if video:
        return str(Path(video).with_suffix(".json"))
    return ""


def fetch_raw_detections(svc: SupabaseService, ride_id: str) -> list[dict]:
    return svc.select("raw_detections", "*", ride_id=ride_id)


def reprocess_ride(
    ride: dict,
    svc: SupabaseService,
    blob: BlobStorageService,
    dry_run: bool,
) -> dict[str, Any]:
    ride_id = ride["id"]
    result = {"ride_id": ride_id, "detections_updated": 0, "potholes_reclustered": 0}

    gps_path = resolve_gps_path(ride)
    if not gps_path:
        logger.warning("[%s] No GPS path found, skipping", ride_id[:8])
        return result

    # Download GPS JSON
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        blob.download_file_streaming(gps_path, tmp_path, RAW_DATA_BUCKET)
        with tmp_path.open("r") as f:
            gps_data = json.load(f)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not isinstance(gps_data, list) or not gps_data:
        logger.warning("[%s] GPS JSON is empty or invalid, skipping", ride_id[:8])
        return result

    # Check if already normalized
    if is_already_normalized(gps_data):
        logger.info("[%s] GPS timestamps already normalized, skipping", ride_id[:8])
        return result

    # Normalize timestamps
    normalized_gps = normalize_gps_timestamps(gps_data)

    # Fetch raw detections
    detections = fetch_raw_detections(svc, ride_id)
    if not detections:
        logger.info("[%s] No raw detections found, skipping", ride_id[:8])
        return result

    # Re-interpolate coordinates for each detection
    updated_detections = []
    for det in detections:
        video_ts = det.get("video_timestamp")
        if video_ts is None:
            continue
        try:
            video_ts = float(video_ts)
        except (TypeError, ValueError):
            continue

        coords = interpolate_sample(normalized_gps, video_ts)
        if coords is None:
            continue

        new_lat, new_lng = coords
        old_lat = det.get("lat")
        old_lng = det.get("lng")

        # Only update if coordinates actually changed
        if old_lat is not None and old_lng is not None:
            try:
                if abs(float(old_lat) - new_lat) < 1e-6 and abs(float(old_lng) - new_lng) < 1e-6:
                    continue
            except (TypeError, ValueError):
                pass

        updated_detections.append({
            "id": det["id"],
            "lat": round(new_lat, 6),
            "lng": round(new_lng, 6),
        })

    if not updated_detections:
        logger.info("[%s] All detection coordinates already correct, skipping", ride_id[:8])
        return result

    logger.info("[%s] Updating %d detection coordinates", ride_id[:8], len(updated_detections))

    if not dry_run:
        for det_update in updated_detections:
            svc.update("raw_detections", {"lat": det_update["lat"], "lng": det_update["lng"]}, id=det_update["id"])

    result["detections_updated"] = len(updated_detections)

    # Re-cluster into verified_potholes
    logger.info("[%s] Re-clustering detections...", ride_id[:8])

    # Delete existing verified_potholes for this ride (batch by ride_id)
    existing_potholes = svc.select("verified_potholes", "id", ride_id=ride_id)
    if existing_potholes and not dry_run:
        svc.client.table("verified_potholes").delete().eq("ride_id", ride_id).execute()
        logger.info("[%s] Deleted %d old verified_potholes", ride_id[:8], len(existing_potholes))

    # Re-fetch updated detections for clustering
    if not dry_run:
        updated_dets = fetch_raw_detections(svc, ride_id)
    else:
        # In dry-run mode, simulate updated detections
        updated_dets = []
        for det in detections:
            for upd in updated_detections:
                if det["id"] == upd["id"]:
                    updated_dets.append({**det, "lat": upd["lat"], "lng": upd["lng"]})
                    break
            else:
                updated_dets.append(det)

    # Inline DBSCAN clustering (avoids importing core.clusterer which uses relative imports)
    clustered = []
    if updated_dets:
        df = pd.DataFrame(updated_dets)
        if "lat" in df.columns and "lng" in df.columns and len(df) >= CLUSTER_MIN_DETECTIONS:
            coords = np.radians(df[["lat", "lng"]].values)
            eps_rad = MERGE_RADIUS_METERS / EARTH_RADIUS_METERS
            db = DBSCAN(eps=eps_rad, min_samples=CLUSTER_MIN_DETECTIONS, metric="haversine")
            db.fit(coords)
            df["cluster_id"] = db.labels_
            for cid in df["cluster_id"].unique():
                if cid == -1:
                    continue
                subset = df[df["cluster_id"] == cid]
                clustered.append({
                    "lat": round(float(subset["lat"].mean()), 6),
                    "lng": round(float(subset["lng"].mean()), 6),
                    "detection_count": len(subset),
                })

    if clustered and not dry_run:
        for pothole in clustered:
            try:
                svc.insert("verified_potholes", {
                    "ride_id": ride_id,
                    "consolidated_latitude": pothole["lat"],
                    "consolidated_longitude": pothole["lng"],
                    "worst_severity": pothole.get("worst_severity", "Minor"),
                    "total_detection_hits": pothole.get("detection_count", 0),
                    "status": "queued",
                    "caption": f"Reprocessed: {pothole.get('detection_count', 0)} detections",
                })
            except Exception as exc:
                logger.warning("[%s] Failed to insert verified_pothole: %s", ride_id[:8], exc)

    result["potholes_reclustered"] = len(clustered)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Reprocess GPS coordinates for existing rides")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max rides to process")
    parser.add_argument("--ride-id", type=str, default=None, help="Process a single ride by ID")
    args = parser.parse_args()

    svc = get_supabase_service()
    blob = get_blob_storage_service()

    logger.info("Fetching completed rides...")
    rides = fetch_completed_rides(svc, args.limit, args.ride_id)
    logger.info("Found %d completed rides", len(rides))

    if not rides:
        logger.info("Nothing to do.")
        return

    total_updated = 0
    total_reclustered = 0
    skipped = 0
    failed = 0

    for i, ride in enumerate(rides, 1):
        ride_id = ride["id"]
        try:
            result = reprocess_ride(ride, svc, blob, args.dry_run)
            if result["detections_updated"] > 0:
                total_updated += result["detections_updated"]
                total_reclustered += result["potholes_reclustered"]
                logger.info(
                    "[%d/%d] ride=%s — updated %d detections, reclustered %d potholes",
                    i, len(rides), ride_id[:8],
                    result["detections_updated"], result["potholes_reclustered"],
                )
            else:
                skipped += 1
                logger.info("[%d/%d] ride=%s — no changes needed", i, len(rides), ride_id[:8])
        except Exception as exc:
            failed += 1
            logger.error("[%d/%d] ride=%s — FAILED: %s", i, len(rides), ride_id[:8], exc)

    logger.info(
        "Done. updated=%d detections, reclustered=%d potholes, skipped=%d, failed=%d, rides=%d",
        total_updated, total_reclustered, skipped, failed, len(rides),
    )


if __name__ == "__main__":
    main()
