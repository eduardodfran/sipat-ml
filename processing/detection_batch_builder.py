from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
from supabase import Client
from ultralytics import YOLO

from .utils.damage_severity import calculate_severity
from .utils.gps_processor import GPSProcessor
from .utils.ipm_transformer import IPMTransformer

ANNOTATED_FRAMES_BUCKET = "detected-images"


class DetectionBatchBuilder:
    def __init__(
        self,
        ride_id: str,
        user_id: str | None,
        supabase: Client,
        model: YOLO,
        supabase_url: str,
    ) -> None:
        self.ride_id = ride_id
        self.user_id = user_id
        self.supabase = supabase
        self.model = model
        self.supabase_url = supabase_url

    def build(
        self, video_path: Path, gps_processor: GPSProcessor
    ) -> list[dict[str, Any]]:
        is_stationary = gps_processor.is_stationary()
        if is_stationary:
            gps_processor = GPSProcessor(gps_processor.collapse_to_median())
            median_lat, median_lng = gps_processor.median_coordinate()
            print(
                f"Stationary GPS track detected — collapsing all detections "
                f"to median coordinate ({median_lat}, {median_lng})"
            )

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open downloaded video: {video_path}")

        try:
            fps = capture.get(cv2.CAP_PROP_FPS)
            if not fps or fps <= 0:
                fps = 30.0

            raw_detections_batch: list[dict[str, Any]] = []
            ipm: IPMTransformer | None = None
            frame_count = 0
            detection_count = 0
            last_image_url: str | None = None

            while capture.isOpened():
                success, frame = capture.read()
                if not success:
                    break

                frame_count += 1
                current_frame_index = capture.get(cv2.CAP_PROP_POS_FRAMES)
                timestamp_seconds = current_frame_index / fps
                if ipm is None:
                    ipm = IPMTransformer(frame.shape[1], frame.shape[0])
                results = self.model(frame, conf=0.4, verbose=False)

                for result in results:
                    if not getattr(result, "boxes", None):
                        continue

                    detection_count += 1

                    try:
                        annotated_frame = result.plot()
                        _, encoded_frame = cv2.imencode(".jpg", annotated_frame)
                        last_image_url = self._upload_annotated_frame(
                            encoded_frame.tobytes(), timestamp_seconds
                        )
                    except Exception as upload_err:
                        print(f"Failed to upload annotated frame: {upload_err}")
                    image_url = last_image_url

                    for _box in result.boxes:
                        severity = "Minor"
                        phys_area_m2 = 0.0
                        try:
                            bbox = _box.xyxyn[0].tolist()
                            severity = calculate_severity(bbox)
                            phys_area_m2 = ipm.compute_phys_area(bbox)
                        except Exception:
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
                        raw_detections_batch.append(
                            {
                                "ride_id": self.ride_id,
                                "user_id": self.user_id,
                                "lat": lat,
                                "lng": lng,
                                "video_timestamp": timestamp_seconds,
                                "severity": severity,
                                "phys_area_m2": phys_area_m2,
                                "image_url": image_url,
                            }
                        )

            print(
                f"Processed {frame_count} frames, "
                f"{detection_count} detections with boxes"
            )
            return raw_detections_batch
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
