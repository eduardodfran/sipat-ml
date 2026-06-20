from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


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
