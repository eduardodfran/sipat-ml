from __future__ import annotations

import json
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from typing import Any


DEFAULT_CALIBRATION_WIDTH = 1920
DEFAULT_CALIBRATION_HEIGHT = 1080


@dataclass
class CameraCalibration:
    fx: float
    fy: float
    cx: float
    cy: float
    height_m: float
    pitch_deg: float
    roll_deg: float = 0.0
    yaw_deg: float = 0.0

    @classmethod
    def from_json(cls, path: str | Path) -> CameraCalibration:
        with open(path) as f:
            data = json.load(f)
        return cls(**{k: data[k] for k in cls.__annotations__ if k in data})

    def to_json(self, path: str | Path) -> None:
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    def scaled_to_resolution(
        self,
        width_px: int,
        height_px: int,
        base_width_px: int = DEFAULT_CALIBRATION_WIDTH,
        base_height_px: int = DEFAULT_CALIBRATION_HEIGHT,
        zoom_factor: float = 1.0,
    ) -> "CameraCalibration":
        """Return a calibration scaled to a different capture resolution.

        The generic calibration file is authored against a baseline 1920x1080
        image.  When recordings come in at 720p, 1080p, 4K, etc., all linear
        intrinsics (fx/fy/cx/cy) must be rescaled proportionally so that the
        perspective projection and IPM area estimates remain geometrically
        consistent.

        A ``zoom_factor`` > 1.0 (e.g. 2× pinch-zoom) increases the effective
        focal lengths because the same scene is projected onto a larger pixel
        area.  Principal point is not affected by zoom.
        """
        if width_px <= 0 or height_px <= 0:
            return self
        if base_width_px <= 0 or base_height_px <= 0:
            return self

        sx = width_px / base_width_px
        sy = height_px / base_height_px
        zoom = max(0.01, float(zoom_factor))

        return replace(
            self,
            fx=float(self.fx) * sx * zoom,
            fy=float(self.fy) * sy * zoom,
            cx=float(self.cx) * sx,
            cy=float(self.cy) * sy,
        )


def load_calibration(model_name: str | None = None) -> CameraCalibration:
    config_dir = Path(__file__).resolve().parent.parent / "config" / "calibrations"
    if model_name:
        path = config_dir / f"{model_name}.json"
        if path.exists():
            return CameraCalibration.from_json(path)
    fallback = config_dir / "generic.json"
    if fallback.exists():
        return CameraCalibration.from_json(fallback)
    return CameraCalibration(
        fx=1200.0, fy=1200.0,
        cx=960.0, cy=540.0,
        height_m=1.3,
        pitch_deg=6.0,
    )
