import base64
import json
import os
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

from config.settings import (
    MODEL_PATH,
    MERGE_RADIUS_METERS,
    CLUSTER_MIN_DETECTIONS,
    RAW_DATA_BUCKET,
)
from core.clusterer import PotholeClusterer
from core.severity import area_to_severity, fuse_severity, escalate_severity
from detection_batch_builder import DetectionBatchBuilder
from utils.geo_math import haversine_distance_meters
from utils.gps_processor import GPSProcessor

load_dotenv()


class RideProcessor:
    """Orchestrates end-to-end processing of a single ride video.

    Handles: claiming rides → downloading assets → YOLO detection →
    clustering → syncing with DB → marking completion.
    """

    def __init__(self, supabase_url: str | None = None, service_key: str | None = None) -> None:
        self.supabase_url = (supabase_url or os.getenv("SUPABASE_URL") or "").strip()
        self.service_key = (service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        self.azure_conn_str = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()

        if not self.supabase_url or not self.service_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

        self._validate_config()
        self._supabase: Client = create_client(self.supabase_url, self.service_key)
        self._blob_service: BlobServiceClient | None = None
        self._model: YOLO | None = None

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
        if self.service_key.count(".") < 2:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY does not look like a Supabase JWT")
        url_ref = self._project_ref_from_url(self.supabase_url)
        payload = self._jwt_payload(self.service_key) or {}
        key_ref = payload.get("ref") if isinstance(payload.get("ref"), str) else None
        role = payload.get("role") if isinstance(payload.get("role"), str) else None
        if role and role != "service_role":
            raise ValueError(f"SUPABASE_SERVICE_ROLE_KEY role is {role!r}, expected service_role")
        if url_ref and key_ref and url_ref != key_ref:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY belong to different projects")

    # ---- blob storage ----

    @property
    def blob_service(self) -> BlobServiceClient:
        if self._blob_service is None:
            if not self.azure_conn_str:
                raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")
            self._blob_service = BlobServiceClient.from_connection_string(self.azure_conn_str)
        return self._blob_service

    def _download_file(self, object_path: str, temp_dir: Path) -> Path:
        blob_client = self.blob_service.get_blob_client(container=RAW_DATA_BUCKET, blob=object_path)
        download_stream = blob_client.download_blob(timeout=120)
        local_path = temp_dir / Path(object_path).name
        local_path.write_bytes(download_stream.readall())
        return local_path

    # ---- model ----

    @property
    def model(self) -> YOLO:
        if self._model is None:
            path = MODEL_PATH
            if not path.exists():
                raise FileNotFoundError(f"YOLO weights not found at {path}")
            self._model = YOLO(str(path))
        return self._model

    # ---- ride querying ----

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
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-c", "copy", str(repaired)],
                capture_output=True, timeout=300,
            )
            if repaired.exists() and repaired.stat().st_size > 0:
                return repaired
        except Exception as e:
            print(f"Video repair failed, using original: {e}")
        return video_path

    # ---- DB operations ----

    def claim_oldest_queued_ride(self) -> dict[str, Any] | None:
        response = (
            self._supabase.table("rides_metadata")
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
        self._supabase.table("rides_metadata").update({"status": "processing"}).eq("id", ride_id).execute()
        ride["status"] = "processing"
        return ride

    def _mark_failed(self, ride_id: str, error_message: str) -> None:
        self._supabase.table("rides_metadata").update(
            {"status": "failed", "error_log": error_message}
        ).eq("id", ride_id).execute()

    def _mark_completed(self, ride_id: str) -> None:
        self._supabase.table("rides_metadata").update({"status": "completed"}).eq("id", ride_id).execute()

    def _insert_raw_detections(self, raw_batch: list[dict[str, Any]]) -> None:
        if not raw_batch:
            return
        try:
            self._supabase.table("raw_detections").insert(raw_batch).execute()
        except APIError as e:
            raise RuntimeError(self._friendly_error(e, "inserting raw_detections")) from e

    def _fetch_verified_potholes(self) -> list[dict[str, Any]]:
        response = self._supabase.table("verified_potholes").select("*").execute()
        return response.data or []

    @staticmethod
    def _friendly_error(exc: Exception, action: str) -> str:
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
            print("No stable pothole clusters produced from this ride")
            return 0

        existing = self._fetch_verified_potholes()
        touched = 0

        for pothole in clustered:
            lat = float(pothole["lat"])
            lng = float(pothole["lng"])
            new_hits = int(pothole.get("detection_count") or 0)
            ipm_sev = area_to_severity(pothole.get("max_area_m2"))
            frame_sev = pothole.get("max_frame_severity", "Minor")
            conf = pothole.get("avg_confidence", 0.0)
            final_sev = fuse_severity(ipm_sev, frame_sev, conf)

            print(f"  severity: ipm={ipm_sev}, frame={frame_sev}, conf={conf:.3f} → final={final_sev}")

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
                self._supabase.schema("public").from_("verified_potholes").update({
                    "total_detection_hits": current_hits + new_hits,
                    "worst_severity": merged_sev,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "user_detections": merged_users,
                }).eq("id", match_id).execute()
                match["total_detection_hits"] = current_hits + new_hits
                match["worst_severity"] = merged_sev
                match["user_detections"] = merged_users
            else:
                self._supabase.schema("public").from_("verified_potholes").insert({
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

        print(f"Synced {touched} verified potholes for ride {ride_id}")
        return touched

    # ---- main process ----

    def process_ride(self, ride: dict[str, Any]) -> dict[str, Any]:
        ride_id = str(ride.get("id") or "")
        if not ride_id:
            raise KeyError("Ride row has no id column")

        video_path = self._resolve_video_path(ride)
        gps_path = self._resolve_gps_path(ride, video_path)
        user_id = ride.get("user_id")

        with tempfile.TemporaryDirectory(prefix=f"ride_{ride_id}_") as tmp:
            tmp_dir = Path(tmp)
            video_local = self._download_file(video_path, tmp_dir)
            video_local = self._repair_video(video_local)
            gps_local = self._download_file(gps_path, tmp_dir)

            gps_processor = GPSProcessor.from_json_file(gps_local)
            builder = DetectionBatchBuilder(
                ride_id=ride_id,
                user_id=str(user_id) if user_id is not None else None,
                supabase=self._supabase,
                model=self.model,
                supabase_url=self.supabase_url,
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
            print(f"Finished ride {ride_id}: {len(raw_batch)} detections")
            return result

    def process_next_queued(self) -> dict[str, Any] | None:
        ride = self.claim_oldest_queued_ride()
        if ride is None:
            print("No queued rides found")
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
                print(f"Failed to mark ride {ride_id} as failed: {mark_exc}")
            raise RuntimeError(error) from exc

    def process_by_id(self, ride_id: str) -> dict[str, Any]:
        response = self._supabase.table("rides_metadata").select("*").eq("id", ride_id).execute()
        rows = response.data or []
        if not rows:
            raise KeyError(f"Ride {ride_id} not found")
        ride = rows[0]
        self._supabase.table("rides_metadata").update({"status": "processing"}).eq("id", ride_id).execute()
        ride["status"] = "processing"
        try:
            return self.process_ride(ride)
        except (APIError, StorageException, OSError, ValueError, KeyError) as exc:
            error = f"{self._friendly_error(exc, 'processing ride')}\n{traceback.format_exc()}"
            try:
                self._mark_failed(ride_id, error)
            except Exception as mark_exc:
                print(f"Failed to mark ride {ride_id} as failed: {mark_exc}")
            raise RuntimeError(error) from exc
