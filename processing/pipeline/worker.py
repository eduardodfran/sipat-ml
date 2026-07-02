import asyncio
import logging
import subprocess
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from postgrest.exceptions import APIError
from storage3.utils import StorageException
from ultralytics import YOLO

from ..config.settings import (
    MAX_WORKERS,
    MODEL_PATH,
    MERGE_RADIUS_METERS,
    RAW_DATA_BUCKET,
)
from ..core.clusterer import PotholeClusterer
from ..core.severity import area_to_severity, fuse_severity, escalate_severity
from ..detection_batch_builder import DetectionBatchBuilder
from ..services.blob_storage import BlobStorageService, get_blob_storage_service
from ..services.supabase_client import SupabaseService, get_supabase_service
from ..utils.geo_math import haversine_distance_meters
from ..utils.gps_processor import GPSProcessor

logger = logging.getLogger(__name__)

_thread_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)


class RideProcessor:
    """Orchestrates end-to-end processing of a single ride video.

    Handles: claiming rides → downloading assets → YOLO detection →
    clustering → syncing with DB → marking completion.
    """

    def __init__(
        self,
        supabase: SupabaseService | None = None,
        blob: BlobStorageService | None = None,
    ) -> None:
        self._svc = supabase or get_supabase_service()
        self._blob = blob or get_blob_storage_service()
        self._model: YOLO | None = None

    @property
    def _supabase(self):
        """Direct Supabase client for DetectionBatchBuilder compatibility."""
        return self._svc.client

    # ---- model ----

    @property
    def model(self) -> YOLO:
        if self._model is None:
            path = MODEL_PATH
            if not path.exists():
                raise FileNotFoundError(f"YOLO weights not found at {path}")
            self._model = YOLO(str(path))
        return self._model

    # ---- ride path resolution ----

    @staticmethod
    def _first_present_value(row: dict[str, Any], keys: list[str]) -> str | None:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _resolve_video_path(row: dict[str, Any]) -> str:
        path = RideProcessor._first_present_value(row, [
            "video_bucket_path", "video_path", "video_uri", "video_file_path",
            "video_file_uri", "video_object_path", "videoName", "video_name",
            "file_name", "filename",
        ])
        if path:
            return path
        raise KeyError("Could not determine video path from rides_metadata row")

    @staticmethod
    def _resolve_gps_path(row: dict[str, Any], video_path: str) -> str:
        path = RideProcessor._first_present_value(row, [
            "gps_bucket_path", "gps_json_path", "gps_path", "gps_uri",
            "gps_file_path", "gps_file_uri", "gps_object_path", "gps_log_path",
            "csv_path", "csv_uri", "metadata_path",
        ])
        if path:
            return str(Path(path).with_suffix(".json")) if path.lower().endswith(".csv") else path
        return str(Path(video_path).with_suffix(".json"))

    @staticmethod
    def _repair_video(video_path: Path) -> Path:
        repaired = video_path.parent / f"repaired_{video_path.name}"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-c", "copy", str(repaired)],
                capture_output=True, timeout=300,
            )
            if repaired.exists() and repaired.stat().st_size > 0:
                return repaired
        except Exception as e:
            logger.warning("Video repair failed, using original: %s", e)
        return video_path

    # ---- DB operations ----

    def claim_oldest_queued_ride(self) -> dict[str, Any] | None:
        svc = self._svc
        client = svc.client
        response = (
            client.table("rides_metadata")
            .select("*")
            .eq("status", "queued")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        ride = rows[0]
        ride_id = ride.get("id")
        if not ride_id:
            raise KeyError("Claimed ride row has no id column")
        svc.update("rides_metadata", {"status": "processing"}, id=ride_id)
        ride["status"] = "processing"
        return ride

    def _mark_failed(self, ride_id: str, error_message: str) -> None:
        self._svc.update("rides_metadata", {"status": "failed", "error_log": error_message}, id=ride_id)

    def _mark_completed(self, ride_id: str) -> None:
        self._svc.update("rides_metadata", {"status": "completed"}, id=ride_id)

    def _insert_raw_detections(self, raw_batch: list[dict[str, Any]]) -> None:
        if not raw_batch:
            return
        try:
            self._svc.insert("raw_detections", raw_batch)
        except APIError as e:
            raise RuntimeError(self._friendly_error(e, "inserting raw_detections")) from e

    def _fetch_verified_potholes(self) -> list[dict[str, Any]]:
        return self._svc.select("verified_potholes")

    @staticmethod
    def _friendly_error(exc: Exception, _: str) -> str:
        if isinstance(exc, APIError) and exc.args:
            payload = exc.args[0]
            if isinstance(payload, dict):
                message = payload.get("message") or str(exc)
                hint = payload.get("hint")
                code = payload.get("code")
                parts = [str(message)]
                if hint:
                    parts.append(f"hint={hint}")
                if code:
                    parts.append(f"code={code}")
                return " | ".join(parts)
        return str(exc)

    @staticmethod
    def _find_matching_pothole(
        potholes: list[dict[str, Any]],
        lat: float,
        lng: float,
        radius: float = MERGE_RADIUS_METERS,
    ) -> dict[str, Any] | None:
        closest = None
        closest_dist = radius
        for p in potholes:
            pl = p.get("consolidated_latitude")
            pn = p.get("consolidated_longitude")
            if pl is None or pn is None:
                continue
            d = haversine_distance_meters(float(pl), float(pn), lat, lng)
            if d <= closest_dist:
                closest_dist = d
                closest = p
        return closest

    @staticmethod
    def _merge_user_detections(
        existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        best: dict[str, float] = {}
        for entry in existing + incoming:
            uid = entry.get("user_id")
            ts = entry.get("video_timestamp")
            if uid is None or ts is None:
                continue
            if uid not in best or ts < best[uid]:
                best[uid] = ts
        return sorted(
            [{"user_id": uid, "video_timestamp": ts} for uid, ts in best.items()],
            key=lambda x: x["video_timestamp"],
        )

    def _sync_potholes(self, raw_batch: list[dict[str, Any]], ride_id: str) -> int:
        clusterer = PotholeClusterer()
        clustered = clusterer.cluster(raw_batch)
        if not clustered:
            logger.info("No stable pothole clusters produced from this ride")
            return 0

        existing = self._fetch_verified_potholes()
        touched = 0
        client = self._svc.client

        for pothole in clustered:
            lat = float(pothole["lat"])
            lng = float(pothole["lng"])

            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                logger.warning(
                    "Skipping pothole with invalid coordinates: lat=%s, lng=%s (ride %s)",
                    lat, lng, ride_id,
                )
                continue

            new_hits = int(pothole.get("detection_count") or 0)
            ipm_sev = area_to_severity(pothole.get("max_area_m2"))
            frame_sev = pothole.get("max_frame_severity", "Minor")
            conf = pothole.get("avg_confidence", 0.0)
            final_sev = fuse_severity(ipm_sev, frame_sev, conf)

            logger.debug("severity: ipm=%s, frame=%s, conf=%.3f -> final=%s", ipm_sev, frame_sev, conf, final_sev)

            match = self._find_matching_pothole(existing, lat, lng)

            if match:
                match_id = match.get("id")
                current_hits = int(match.get("total_detection_hits") or 0)
                current_sev = match.get("worst_severity") or "Minor"
                merged_sev = escalate_severity(current_sev, final_sev)
                merged_users = self._merge_user_detections(
                    match.get("user_detections") if isinstance(match.get("user_detections"), list) else [],
                    pothole.get("user_detections") or [],
                )
                client.schema("public").from_("verified_potholes").update({
                    "total_detection_hits": current_hits + new_hits,
                    "worst_severity": merged_sev,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "user_detections": merged_users,
                }).eq("id", match_id).execute()
                match["total_detection_hits"] = current_hits + new_hits
                match["worst_severity"] = merged_sev
                match["user_detections"] = merged_users
            else:
                client.schema("public").from_("verified_potholes").insert({
                    "ride_id": ride_id,
                    "consolidated_latitude": lat,
                    "consolidated_longitude": lng,
                    "worst_severity": final_sev,
                    "total_detection_hits": new_hits,
                    "status": "queued",
                    "user_detections": pothole.get("user_detections") or [],
                }).execute()
                existing.append({
                    "id": None,
                    "consolidated_latitude": lat,
                    "consolidated_longitude": lng,
                    "worst_severity": final_sev,
                    "total_detection_hits": new_hits,
                    "status": "queued",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "user_detections": pothole.get("user_detections") or [],
                })
            touched += 1

        logger.info("Synced %d verified potholes for ride %s", touched, ride_id)
        return touched

    # ---- main process ----

    async def process_ride_async(self, ride: dict[str, Any]) -> dict[str, Any]:
        """Run process_ride in a thread pool to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_thread_pool, self.process_ride, ride)

    def process_ride(self, ride: dict[str, Any]) -> dict[str, Any]:
        ride_id = str(ride.get("id") or "")
        if not ride_id:
            raise KeyError("Ride row has no id column")

        video_path = self._resolve_video_path(ride)
        gps_path = self._resolve_gps_path(ride, video_path)
        user_id = ride.get("user_id")

        with tempfile.TemporaryDirectory(prefix=f"ride_{ride_id}_") as tmp:
            tmp_dir = Path(tmp)

            video_bytes = self._blob.download_file(video_path, RAW_DATA_BUCKET)
            video_local = tmp_dir / Path(video_path).name
            video_local.write_bytes(video_bytes)
            video_local = self._repair_video(video_local)

            gps_bytes = self._blob.download_file(gps_path, RAW_DATA_BUCKET)
            gps_local = tmp_dir / Path(gps_path).name
            gps_local.write_bytes(gps_bytes)

            gps_processor = GPSProcessor.from_json_file(gps_local)
            builder = DetectionBatchBuilder(
                ride_id=ride_id,
                user_id=str(user_id) if user_id is not None else None,
                supabase=self._supabase,
                model=self.model,
                supabase_url=self._svc.url,
            )
            raw_batch = builder.build(video_local, gps_processor)
            self._insert_raw_detections(raw_batch)
            self._sync_potholes(raw_batch, ride_id)
            self._mark_completed(ride_id)

            result = {
                "ride_id": ride_id,
                "video_path": str(video_local),
                "gps_path": str(gps_local),
                "raw_detection_count": len(raw_batch),
                "source_video_object": video_path,
                "source_gps_object": gps_path,
            }
            logger.info("Finished ride %s: %d detections", ride_id, len(raw_batch))
            return result

    def process_next_queued(self) -> dict[str, Any] | None:
        ride = self.claim_oldest_queued_ride()
        if ride is None:
            logger.info("No queued rides found")
            return None
        ride_id = str(ride.get("id") or "")
        if not ride_id:
            raise KeyError("Claimed ride row has no id column")
        try:
            return self.process_ride(ride)
        except (APIError, StorageException, OSError, ValueError, KeyError) as exc:
            error = f"{self._friendly_error(exc, 'processing ride')}\n{traceback.format_exc()}"
            try:
                self._mark_failed(ride_id, error)
            except Exception as mark_exc:
                logger.error("Failed to mark ride %s as failed: %s", ride_id, mark_exc)
            raise RuntimeError(error) from exc

    def process_by_id(self, ride_id: str) -> dict[str, Any]:
        rows = self._svc.select("rides_metadata", "*", id=ride_id)
        if not rows:
            raise KeyError(f"Ride {ride_id} not found")
        ride = rows[0]
        self._svc.update("rides_metadata", {"status": "processing"}, id=ride_id)
        ride["status"] = "processing"
        try:
            return self.process_ride(ride)
        except (APIError, StorageException, OSError, ValueError, KeyError) as exc:
            error = f"{self._friendly_error(exc, 'processing ride')}\n{traceback.format_exc()}"
            try:
                self._mark_failed(ride_id, error)
            except Exception as mark_exc:
                logger.error("Failed to mark ride %s as failed: %s", ride_id, mark_exc)
            raise RuntimeError(error) from exc
