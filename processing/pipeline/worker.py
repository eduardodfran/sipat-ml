import asyncio
import logging
import math
import subprocess
import tempfile
import threading
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
    MAX_CONCURRENT_RIDES,
    RIDE_PROCESS_TIMEOUT,
    BATCH_INSERT_SIZE,
    MODEL_PATH,
    MERGE_RADIUS_METERS,
    RAW_DATA_BUCKET,
)
from ..core.clusterer import PotholeClusterer
from ..core.severity import area_to_severity, fuse_severity, escalate_severity
from ..detection_batch_builder import DetectionBatchBuilder
from ..services.blob_storage import BlobStorageService, get_blob_storage_service
from ..services.geocoder import geocode_pothole
from ..services.supabase_client import SupabaseService, get_supabase_service
from ..utils.geo_math import haversine_distance_meters
from ..utils.gps_processor import GPSProcessor

logger = logging.getLogger(__name__)

_thread_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
_concurrency_semaphore = threading.Semaphore(MAX_CONCURRENT_RIDES)


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
        result = (
            client.table("rides_metadata")
            .update({"status": "processing"})
            .eq("id", ride_id)
            .eq("status", "queued")
            .execute()
        )
        if not result.data:
            logger.info("Ride %s already claimed by another worker, skipping", ride_id)
            return None
        ride["status"] = "processing"
        return ride

    def _mark_failed(self, ride_id: str, error_message: str) -> None:
        self._svc.update("rides_metadata", {"status": "failed", "error_log": error_message}, id=ride_id)

    def _mark_completed(self, ride_id: str) -> None:
        self._svc.update("rides_metadata", {"status": "completed", "progress_pct": 100, "progress_stage": "done", "progress_message": "Processing complete"}, id=ride_id)

    def _update_progress(self, ride_id: str, pct: int, stage: str, message: str) -> None:
        print(f"[DIAG] _update_progress({ride_id[:8]}, pct={pct}, stage={stage})", flush=True)
        try:
            result = self._svc.update("rides_metadata", {
                "progress_pct": pct,
                "progress_stage": stage,
                "progress_message": message,
            }, id=ride_id)
            print(f"[DIAG] _update_progress OK for {ride_id[:8]} pct={pct}", flush=True)
        except Exception as exc:
            print(f"[DIAG] _update_progress FAILED for {ride_id[:8]} pct={pct}: {exc}", flush=True)
            logger.warning("[%s] Progress update failed (pct=%s stage=%s): %s", ride_id[:8], pct, stage, exc)

    def _insert_raw_detections(self, raw_batch: list[dict[str, Any]]) -> None:
        if not raw_batch:
            return
        try:
            for i in range(0, len(raw_batch), BATCH_INSERT_SIZE):
                chunk = raw_batch[i : i + BATCH_INSERT_SIZE]
                self._svc.insert("raw_detections", chunk)
        except APIError as e:
            raise RuntimeError(self._friendly_error(e, "inserting raw_detections")) from e

    @staticmethod
    def _approx_degree_delta(meters: float, lat: float) -> tuple[float, float]:
        dlat = meters / 111_320.0
        dlng = meters / (111_320.0 * math.cos(math.radians(lat)))
        return dlat, dlng

    def _fetch_nearby_potholes(self, lat: float, lng: float) -> list[dict[str, Any]]:
        try:
            radius = MERGE_RADIUS_METERS * 2.0
            dlat, dlng = self._approx_degree_delta(radius, lat)
            result = (
                self._svc.client.table("verified_potholes")
                .select("*")
                .gte("consolidated_latitude", lat - dlat)
                .lte("consolidated_latitude", lat + dlat)
                .gte("consolidated_longitude", lng - dlng)
                .lte("consolidated_longitude", lng + dlng)
                .execute()
            )
            return result.data or []
        except Exception:
            logger.warning("Bounding-box query failed, falling back to full fetch", exc_info=True)
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

    def _generate_caption(self, severity: str, hits: int, area_m2: float | None) -> str:
        parts = [f"{severity} pothole detected"]
        detail = []
        if hits:
            detail.append(f"{hits} report{'s' if hits != 1 else ''}")
        if area_m2 and area_m2 > 0:
            detail.append(f"{area_m2:.2f} m\u00b2")
        if detail:
            parts[0] += ". " + ", ".join(detail)
        else:
            parts[0] += "."
        return parts[0]

    def _sync_potholes(self, raw_batch: list[dict[str, Any]], ride_id: str) -> int:
        clusterer = PotholeClusterer()
        clustered = clusterer.cluster(raw_batch)
        if not clustered:
            logger.info("No stable pothole clusters produced from this ride")
            return 0

        touched = 0
        existing: list[dict[str, Any]] = []
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

            nearby = self._fetch_nearby_potholes(lat, lng)
            match = self._find_matching_pothole(nearby + existing, lat, lng)

            if match:
                match_id = match.get("id")
                current_hits = int(match.get("total_detection_hits") or 0)
                current_sev = match.get("worst_severity") or "Minor"
                merged_sev = escalate_severity(current_sev, final_sev)
                merged_users = self._merge_user_detections(
                    match.get("user_detections") if isinstance(match.get("user_detections"), list) else [],
                    pothole.get("user_detections") or [],
                )
                new_total_hits = current_hits + new_hits
                existing_area = match.get("max_area_m2") or pothole.get("max_area_m2")
                new_caption = self._generate_caption(merged_sev, new_total_hits, existing_area)
                client.schema("public").from_("verified_potholes").update({
                    "total_detection_hits": new_total_hits,
                    "worst_severity": merged_sev,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "user_detections": merged_users,
                    "caption": new_caption,
                }).eq("id", match_id).execute()
                match["total_detection_hits"] = current_hits + new_hits
                match["worst_severity"] = merged_sev
                match["user_detections"] = merged_users
            else:
                area_m2 = pothole.get("max_area_m2")
                caption = self._generate_caption(final_sev, new_hits, area_m2)
                result = client.schema("public").from_("verified_potholes").insert({
                    "ride_id": ride_id,
                    "consolidated_latitude": lat,
                    "consolidated_longitude": lng,
                    "worst_severity": final_sev,
                    "total_detection_hits": new_hits,
                    "status": "queued",
                    "user_detections": pothole.get("user_detections") or [],
                    "caption": caption,
                }).execute()
                new_id = result.data[0]["id"] if result.data else None
                existing.append({
                    "id": new_id,
                    "consolidated_latitude": lat,
                    "consolidated_longitude": lng,
                    "worst_severity": final_sev,
                    "total_detection_hits": new_hits,
                    "status": "queued",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "user_detections": pothole.get("user_detections") or [],
                })
                # Geocode address in background (best-effort, non-blocking)
                if new_id is not None:
                    try:
                        geocode_pothole(client, new_id, lat, lng)
                    except Exception as geo_exc:
                        logger.warning("Geocoding failed for pothole %s: %s", new_id, geo_exc)
            touched += 1

        logger.info("Synced %d verified potholes for ride %s", touched, ride_id)
        return touched

    # ---- main process ----

    async def process_ride_async(self, ride: dict[str, Any]) -> dict[str, Any]:
        """Run process_ride in a thread pool with concurrency limit and timeout."""
        loop = asyncio.get_running_loop()

        def _guarded():
            with _concurrency_semaphore:
                return self.process_ride(ride)

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_thread_pool, _guarded),
                timeout=RIDE_PROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            ride_id = str(ride.get("id") or "")
            self._mark_failed(ride_id, f"Processing timed out after {RIDE_PROCESS_TIMEOUT}s")
            raise TimeoutError(f"Ride {ride_id} exceeded {RIDE_PROCESS_TIMEOUT}s timeout")

    def process_ride(self, ride: dict[str, Any]) -> dict[str, Any]:
        ride_id = str(ride.get("id") or "")
        if not ride_id:
            raise KeyError("Ride row has no id column")

        video_path = self._resolve_video_path(ride)
        gps_path = self._resolve_gps_path(ride, video_path)
        user_id = ride.get("user_id")

        print(f"[DIAG] process_ride started for {ride_id[:8]}, video={video_path}, gps={gps_path}", flush=True)
        logger.info("[%s] ▶ Starting processing", ride_id[:8])

        with tempfile.TemporaryDirectory(prefix=f"ride_{ride_id}_") as tmp:
            tmp_dir = Path(tmp)

            self._update_progress(ride_id, 5, "downloading", "Downloading video...")
            logger.info("[%s]   1/5 Downloading video...", ride_id[:8])
            video_local = tmp_dir / Path(video_path).name
            self._blob.download_file_streaming(video_path, video_local, RAW_DATA_BUCKET)
            video_local = self._repair_video(video_local)

            self._update_progress(ride_id, 15, "downloading", "Downloading GPS data...")
            logger.info("[%s]   2/5 Downloading GPS data...", ride_id[:8])
            gps_local = tmp_dir / Path(gps_path).name
            self._blob.download_file_streaming(gps_path, gps_local, RAW_DATA_BUCKET)

            self._update_progress(ride_id, 25, "detecting", "Running YOLO detection...")
            logger.info("[%s]   3/5 Running YOLO detection...", ride_id[:8])
            gps_processor = GPSProcessor.from_json_file(gps_local)
            builder = DetectionBatchBuilder(
                ride_id=ride_id,
                user_id=str(user_id) if user_id is not None else None,
                supabase=self._supabase,
                model=self.model,
                supabase_url=self._svc.url,
                progress_callback=lambda pct, stage, msg: self._update_progress(ride_id, pct, stage, msg),
            )
            raw_batch = builder.build(video_local, gps_processor)

            self._update_progress(ride_id, 85, "saving", f"Saving {len(raw_batch)} detections...")
            logger.info("[%s]   4/5 Saving %d detections to database...", ride_id[:8], len(raw_batch))
            self._insert_raw_detections(raw_batch)
            self._sync_potholes(raw_batch, ride_id)

            self._update_progress(ride_id, 95, "finalizing", "Finalizing...")
            logger.info("[%s]   5/5 Finalizing...", ride_id[:8])
            self._mark_completed(ride_id)

            result = {
                "ride_id": ride_id,
                "video_path": str(video_local),
                "gps_path": str(gps_local),
                "raw_detection_count": len(raw_batch),
                "source_video_object": video_path,
                "source_gps_object": gps_path,
            }
            logger.info("[%s] ✓ Done — %d detections found", ride_id[:8], len(raw_batch))
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
        result = (
            self._svc.client.table("rides_metadata")
            .update({"status": "processing"})
            .eq("id", ride_id)
            .eq("status", ride.get("status", ""))
            .execute()
        )
        if not result.data:
            raise RuntimeError(f"Ride {ride_id} status changed during claim, retry needed")
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
