from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .camera_calibration import CameraCalibration

DEFAULT_NEAR_METERS = 3.0
DEFAULT_FAR_METERS = 25.0
DEFAULT_ROAD_WIDTH_METERS = 6.0
DEFAULT_PIXELS_PER_METER = 100.0


@dataclass(frozen=True)
class IPMContext:
    matrix: np.ndarray
    pixels_per_meter: float
    output_width_px: int
    output_height_px: int
    frame_width: int
    frame_height: int


class IPMTransformer:
    def __init__(
        self,
        frame_width: int,
        frame_height: int,
        calibration: CameraCalibration | None = None,
        near_meters: float = DEFAULT_NEAR_METERS,
        far_meters: float = DEFAULT_FAR_METERS,
        road_width_meters: float = DEFAULT_ROAD_WIDTH_METERS,
        pixels_per_meter: float = DEFAULT_PIXELS_PER_METER,
    ) -> None:
        self.context = self._build_context(
            frame_width,
            frame_height,
            calibration,
            near_meters,
            far_meters,
            road_width_meters,
            pixels_per_meter,
        )

    @staticmethod
    def _calibrated_roi_points(
        frame_width: int, frame_height: int, cal: CameraCalibration
    ) -> np.ndarray:
        """Project a road rectangle to image corners using camera parameters."""
        half_w = DEFAULT_ROAD_WIDTH_METERS / 2.0
        near_z = DEFAULT_NEAR_METERS
        far_z = DEFAULT_FAR_METERS
        road_corners = [
            (-half_w, near_z),
            (half_w, near_z),
            (half_w, far_z),
            (-half_w, far_z),
        ]

        pitch_rad = math.radians(cal.pitch_deg)
        roll_rad = math.radians(cal.roll_deg)
        yaw_rad = math.radians(cal.yaw_deg)

        cos_p, sin_p = math.cos(pitch_rad), math.sin(pitch_rad)
        cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)
        cos_y, sin_y = math.cos(yaw_rad), math.sin(yaw_rad)

        img_points = []
        for x_road, z_road in road_corners:
            y_road = 0.0
            X = np.array([x_road, -cal.height_m, z_road], dtype=np.float64)

            R_yaw = np.array([
                [cos_y, 0, sin_y],
                [0, 1, 0],
                [-sin_y, 0, cos_y],
            ])
            X = R_yaw @ X

            R_pitch = np.array([
                [1, 0, 0],
                [0, cos_p, -sin_p],
                [0, sin_p, cos_p],
            ])
            X = R_pitch @ X

            R_roll = np.array([
                [cos_r, -sin_r, 0],
                [sin_r, cos_r, 0],
                [0, 0, 1],
            ])
            X = R_roll @ X

            x_c, y_c, z_c = X
            if z_c <= 0:
                continue
            u = cal.fx * x_c / z_c + cal.cx
            v = cal.fy * y_c / z_c + cal.cy
            img_points.append([u, v])

        if len(img_points) < 4:
            return IPMTransformer._default_roi_points(frame_width, frame_height)

        return np.array(img_points, dtype=np.float32)

    @staticmethod
    def _default_roi_points(frame_width: int, frame_height: int) -> np.ndarray:
        center_x = frame_width * 0.5
        top_y = frame_height * 0.6
        bottom_y = frame_height * 0.95
        top_half_width = frame_width * 0.1
        bottom_half_width = frame_width * 0.45
        return np.array(
            [
                [center_x - top_half_width, top_y],
                [center_x + top_half_width, top_y],
                [center_x + bottom_half_width, bottom_y],
                [center_x - bottom_half_width, bottom_y],
            ],
            dtype=np.float32,
        )

    def _build_context(
        self,
        frame_width: int,
        frame_height: int,
        calibration: CameraCalibration | None,
        near_meters: float,
        far_meters: float,
        road_width_meters: float,
        pixels_per_meter: float,
    ) -> IPMContext:
        if calibration is not None:
            src = self._calibrated_roi_points(frame_width, frame_height, calibration)
        else:
            src = self._default_roi_points(frame_width, frame_height)

        output_width_px = max(1, int(road_width_meters * pixels_per_meter))
        output_height_px = max(1, int(far_meters * pixels_per_meter))
        dst = np.array(
            [
                [0.0, 0.0],
                [float(output_width_px), 0.0],
                [float(output_width_px), float(output_height_px)],
                [0.0, float(output_height_px)],
            ],
            dtype=np.float32,
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

    def pixel_to_offset(
        self, pixel_point: tuple[float, float] | None
    ) -> tuple[float, float]:
        ctx = self.context
        if pixel_point is None:
            return 0.0, 0.0

        x, y = pixel_point
        x = float(np.clip(x, 0.0, ctx.frame_width - 1))
        y = float(np.clip(y, 0.0, ctx.frame_height - 1))

        point = np.array([[[x, y]]], dtype=np.float32)
        transformed = cv2.perspectiveTransform(point, ctx.matrix)
        x_bev, y_bev = transformed[0][0]
        if not np.isfinite(x_bev) or not np.isfinite(y_bev):
            return 0.0, 0.0

        dx_meters = (
            float(x_bev) - (ctx.output_width_px / 2.0)
        ) / ctx.pixels_per_meter
        dy_meters = (
            ctx.output_height_px - float(y_bev)
        ) / ctx.pixels_per_meter

        max_lateral = ctx.output_width_px / (2.0 * ctx.pixels_per_meter)
        max_forward = ctx.output_height_px / ctx.pixels_per_meter
        dx_meters = max(-max_lateral, min(max_lateral, dx_meters))
        dy_meters = max(0.0, min(max_forward, dy_meters))

        return dx_meters, dy_meters

    def compute_phys_area(self, bbox_normalized: list[float]) -> float:
        ctx = self.context
        x1, y1, x2, y2 = bbox_normalized
        corners = np.array(
            [
                [
                    [x1 * ctx.frame_width, y1 * ctx.frame_height],
                    [x2 * ctx.frame_width, y1 * ctx.frame_height],
                    [x2 * ctx.frame_width, y2 * ctx.frame_height],
                    [x1 * ctx.frame_width, y2 * ctx.frame_height],
                ]
            ],
            dtype=np.float32,
        )
        bev_corners = cv2.perspectiveTransform(corners, ctx.matrix)[0]
        bev_w = max(0.0, float(np.max(bev_corners[:, 0]) - np.min(bev_corners[:, 0])))
        bev_h = max(0.0, float(np.max(bev_corners[:, 1]) - np.min(bev_corners[:, 1])))
        return float((bev_w / ctx.pixels_per_meter) * (bev_h / ctx.pixels_per_meter))
