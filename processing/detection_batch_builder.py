from __future__ import annotations

import concurrent.futures
import logging
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from supabase import Client
from ultralytics import YOLO

from .config.settings import (
    ANNOTATED_FRAMES_BUCKET,
    BLUR_THRESHOLD,
    CROP_TOP_RATIO,
    DARK_THRESHOLD,
    EXCLUDED_CLASSES,
    FRAME_SKIP,
    YOLO_CONFIDENCE,
    _IOU_THRESHOLD,
)
from .core.severity import frame_area_pct_to_severity
from .utils.camera_calibration import CameraCalibration, load_calibration
from .utils.gps_processor import GPSProcessor
from .utils.ipm_transformer import IPMTransformer

logger = logging.getLogger(__name__)
_CALIBRATION = load_calibration()

_clahe_local = threading.local()


def _get_clahe() -> cv2.CLAHE:
    if not hasattr(_clahe_local, "instance"):
        _clahe_local.instance = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return _clahe_local.instance


def _apply_clahe(frame: np.ndarray) -> np.ndarray:
    """Enhance contrast via CLAHE on the L channel of LAB color space."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = _get_clahe().apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def _iou(box_a: list[float], box_b: list[float]) -> float:
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


class DetectionBatchBuilder:
    def __init__(
        self,
        ride_id: str,
        user_id: str | None,
        supabase: Client,
        model: YOLO,
        supabase_url: str,
        progress_callback: callable | None = None,
    ) -> None:
        self.ride_id = ride_id
        self.user_id = user_id
        self.supabase = supabase
        self.model = model
        self.supabase_url = supabase_url
        self._progress_callback = progress_callback

    def build(
        self, video_path: Path, gps_processor: GPSProcessor
    ) -> list[dict[str, Any]]:
        is_stationary = gps_processor.is_stationary()
        if is_stationary:
            gps_processor = GPSProcessor(gps_processor.collapse_to_median())
            median_lat, median_lng = gps_processor.median_coordinate()
            logger.info(
                "Stationary GPS track detected — collapsing all detections "
                "to median coordinate (%s, %s)", median_lat, median_lng
            )

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open downloaded video: {video_path}")

        try:
            fps = capture.get(cv2.CAP_PROP_FPS)
            if not fps or fps <= 0:
                fps = 30.0

            total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            total_seconds = total_frames / fps if fps else 0
            processable_frames = total_frames // FRAME_SKIP
            logger.info("Video: %.1fs, %d frames (processing every %dth → %d frames to scan)",
                        total_seconds, total_frames, FRAME_SKIP, processable_frames)

            raw_detections_batch: list[dict[str, Any]] = []
            ipm: IPMTransformer | None = None
            frame_count = 0
            detection_count = 0
            bev_saved = False
            prev_frame_boxes: list[list[float]] = []
            last_progress_log = 0

            # Deferred frame uploads — submitted during YOLO loop, resolved after
            upload_futures: list[concurrent.futures.Future] = []
            upload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

            try:
                while capture.isOpened():
                    success, frame = capture.read()
                    if not success:
                        break

                    frame_count += 1
                    if frame_count % FRAME_SKIP != 0:
                        prev_frame_boxes = []
                        continue

                    current_frame_index = capture.get(cv2.CAP_PROP_POS_FRAMES)
                    timestamp_seconds = current_frame_index / fps

                    h, w = frame.shape[:2]
                    crop_y = 0
                    if h > w and CROP_TOP_RATIO > 0:
                        crop_y = int(h * CROP_TOP_RATIO)
                        frame = frame[crop_y:]

                    if ipm is None:
                        cal = _CALIBRATION
                        if h > w:
                            cal = CameraCalibration(
                                fx=cal.fx, fy=cal.fy,
                                cx=cal.cy,
                                cy=cal.cx - crop_y,
                                height_m=cal.height_m,
                                pitch_deg=cal.pitch_deg,
                                roll_deg=cal.roll_deg,
                                yaw_deg=cal.yaw_deg,
                            )
                        ipm = IPMTransformer(frame.shape[1], frame.shape[0], calibration=cal)

                    # --- frame quality gate: skip blurry / dark frames ---
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
                    mean_brightness = cv2.mean(gray)[0]

                    if laplacian_var < BLUR_THRESHOLD:
                        prev_frame_boxes = []
                        continue

                    if mean_brightness < DARK_THRESHOLD:
                        prev_frame_boxes = []
                        continue

                    # Enhance contrast on clear frames only
                    if laplacian_var > BLUR_THRESHOLD * 2:
                        frame = _apply_clahe(frame)

                    results = self.model(frame, conf=YOLO_CONFIDENCE, verbose=False)

                    processed_count = frame_count // FRAME_SKIP
                    if processed_count > 0 and processed_count % max(1, processable_frames // 10) == 0 and processed_count != last_progress_log:
                        pct = (processed_count / processable_frames) * 100 if processable_frames else 0
                        elapsed_sec = frame_count / fps
                        msg = f"{processed_count}/{processable_frames} frames ({pct:.0f}%)"
                        logger.info("  ▶ Detection: %s | %.1fs elapsed | %d detections so far",
                                    msg, elapsed_sec, detection_count)
                        if self._progress_callback:
                            db_pct = 25 + int(pct * 0.6)  # map 25-85 range
                            self._progress_callback(min(db_pct, 85), "detecting", f"YOLO: {msg}")
                        last_progress_log = processed_count

                    for result in results:
                        if not getattr(result, "boxes", None):
                            prev_frame_boxes = []
                            continue

                        detection_count += 1

                        # Save first detection as original + BEV for thesis proof
                        if not bev_saved and ipm is not None:
                            bev_saved = True
                            try:
                                annotated_frame = result.plot()
                                orig_path = f"annotated-frames/{self.ride_id}/first_detection_original.jpg"
                                bev_img = ipm.warp_to_bev(frame)
                                _, orig_enc = cv2.imencode(".jpg", annotated_frame)
                                _, bev_enc = cv2.imencode(".jpg", bev_img)
                                self.supabase.storage.from_(
                                    ANNOTATED_FRAMES_BUCKET
                                ).upload(
                                    path=orig_path,
                                    file=orig_enc.tobytes(),
                                    file_options={
                                        "content-type": "image/jpeg",
                                        "upsert": "true",
                                    },
                                )
                                bev_path = f"annotated-frames/{self.ride_id}/first_detection_bev.jpg"
                                self.supabase.storage.from_(
                                    ANNOTATED_FRAMES_BUCKET
                                ).upload(
                                    path=bev_path,
                                    file=bev_enc.tobytes(),
                                    file_options={
                                        "content-type": "image/jpeg",
                                        "upsert": "true",
                                    },
                                )
                                logger.info(
                                    "Saved first detection frame + BEV for ride %s",
                                    self.ride_id,
                                )
                            except Exception as bev_err:
                                logger.warning("Failed to save BEV frame: %s", bev_err)

                        try:
                            annotated_frame = result.plot()
                            _, encoded_frame = cv2.imencode(".jpg", annotated_frame)
                            frame_bytes = encoded_frame.tobytes()
                            ts = timestamp_seconds

                            def _upload_frame(fb: bytes = frame_bytes, t: float = ts) -> tuple[float, str]:
                                url = self._upload_annotated_frame(fb, t)
                                return t, url

                            future = upload_executor.submit(_upload_frame)
                            upload_futures.append(future)
                        except Exception as encode_err:
                            logger.warning("Failed to encode annotated frame: %s", encode_err)

                        current_frame_boxes: list[list[float]] = []
                        for _box in result.boxes:
                            try:
                                bbox = _box.xyxyn[0].tolist()
                            except Exception:
                                continue

                            current_frame_boxes.append(bbox)

                            if prev_frame_boxes and any(
                                _iou(bbox, prev) > _IOU_THRESHOLD
                                for prev in prev_frame_boxes
                            ):
                                continue

                            severity = "Minor"
                            phys_area_m2 = 0.0
                            try:
                                severity = frame_area_pct_to_severity(bbox)
                                phys_area_m2 = ipm.compute_phys_area(bbox)
                            except Exception as e:
                                logger.debug("severity/IPM error for bbox %s: %s", bbox, e)
                                pass

                            lat, lng = self._get_detection_coords(
                                gps_processor,
                                ipm,
                                _box,
                                timestamp_seconds,
                                is_stationary,
                                median_lat if is_stationary else None,
                                median_lng if is_stationary else None,
                            )
                            try:
                                confidence = _box.conf.item()
                            except Exception:
                                confidence = 0.0

                            try:
                                class_id = int(_box.cls.item())
                                class_name = result.names.get(class_id, "unknown")
                            except Exception:
                                class_id = -1
                                class_name = "unknown"

                            if class_name in EXCLUDED_CLASSES:
                                continue

                            raw_detections_batch.append(
                                {
                                    "ride_id": self.ride_id,
                                    "user_id": self.user_id,
                                    "lat": lat,
                                    "lng": lng,
                                    "video_timestamp": timestamp_seconds,
                                    "severity": severity,
                                    "phys_area_m2": phys_area_m2,
                                    "image_url": None,
                                    "confidence": confidence,
                                    "bbox_x1": bbox[0],
                                    "bbox_y1": bbox[1],
                                    "bbox_x2": bbox[2],
                                    "bbox_y2": bbox[3],
                                    "class_id": class_id,
                                    "class_name": class_name,
                                }
                            )

                        prev_frame_boxes = current_frame_boxes

                # Wait for all frame uploads to finish, then resolve URLs
                upload_executor.shutdown(wait=True)
                if upload_futures:
                    logger.info("Resolving %d frame uploads...", len(upload_futures))
                    url_by_ts: dict[float, str] = {}
                    for future in upload_futures:
                        try:
                            ts, url = future.result()
                            url_by_ts[ts] = url
                        except Exception as upload_err:
                            logger.warning("Deferred frame upload failed: %s", upload_err)
                    for det in raw_detections_batch:
                        ts = det["video_timestamp"]
                        if ts in url_by_ts:
                            det["image_url"] = url_by_ts[ts]
                    logger.info("Resolved frame uploads for %d unique timestamps", len(url_by_ts))

                total_processed = frame_count // FRAME_SKIP
                logger.info(
                    "Detection complete — %d/%d frames scanned, %d detections found",
                    total_processed, processable_frames, detection_count
                )
                return raw_detections_batch
            finally:
                upload_executor.shutdown(wait=True)
        finally:
            capture.release()

    # ----- annotated frame upload -----

    def _upload_annotated_frame(
        self, frame_bytes: bytes, timestamp_seconds: float
    ) -> str:
        object_path = (
            f"annotated-frames/{self.ride_id}/{timestamp_seconds:.3f}.jpg"
        )
        self.supabase.storage.from_(ANNOTATED_FRAMES_BUCKET).upload(
            path=object_path,
            file=frame_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )
        return (
            f"{self.supabase_url}/storage/v1/object/public/"
            f"{ANNOTATED_FRAMES_BUCKET}/{object_path}"
        )

    def upload_frames_batch(
        self,
        frames: dict[int, Path],
        ride_id: str,
        batch_size: int = 100,
    ) -> dict[int, str]:
        """Upload annotated frames in batches to avoid memory pressure and per-request overhead.

        Args:
            frames: Dict mapping frame index to local file path.
            ride_id: The ride ID for path construction.
            batch_size: Number of frames to upload per batch.

        Returns:
            Dict mapping frame index to blob path.
        """
        import concurrent.futures

        frame_map: dict[int, str] = {}

        sorted_indices = sorted(frames.keys())
        for batch_start in range(0, len(sorted_indices), batch_size):
            batch_indices = sorted_indices[batch_start : batch_start + batch_size]
            batch = {i: frames[i] for i in batch_indices}

            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(self._upload_one_frame, ride_id, idx, path): idx
                    for idx, path in batch.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    idx = futures[future]
                    frame_map[idx] = future.result()

            batch_num = batch_start // batch_size + 1
            total_batches = (len(sorted_indices) + batch_size - 1) // batch_size
            logger.info(
                f"[batch] uploaded frame batch {batch_num}/{total_batches} "
                f"({len(batch)} frames)"
            )

        return frame_map

    def _upload_one_frame(self, ride_id: str, idx: int, path: Path) -> str:
        """Upload a single annotated frame."""
        object_path = f"annotated-frames/{ride_id}/frame_{idx:06d}.jpg"
        self.supabase.storage.from_(ANNOTATED_FRAMES_BUCKET).upload(
            path=object_path,
            file=path.read_bytes(),
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )
        return (
            f"{self.supabase_url}/storage/v1/object/public/"
            f"{ANNOTATED_FRAMES_BUCKET}/{object_path}"
        )

    # ----- detection coordinate resolution -----

    @staticmethod
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

    @staticmethod
    def _get_detection_coords(
        gps_processor: GPSProcessor,
        ipm: IPMTransformer,
        box: Any,
        timestamp_seconds: float,
        is_stationary: bool,
        median_lat: float | None,
        median_lng: float | None,
    ) -> tuple[float, float]:
        if is_stationary and median_lat is not None and median_lng is not None:
            return median_lat, median_lng
        bottom_center = DetectionBatchBuilder._bottom_center_point(box)
        dx_meters, dy_meters = ipm.pixel_to_offset(bottom_center)
        return gps_processor.project_detection_to_gps(
            timestamp_seconds, dx_meters, dy_meters
        )
