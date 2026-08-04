from __future__ import annotations

import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geo_math import (
    EARTH_RADIUS_METERS,
    STATIONARY_THRESHOLD_METERS,
    bearing_degrees,
    haversine_distance_meters,
    is_stationary_gps_track,
)
from .geo_sync import interpolate_coordinate_at_time


@dataclass(frozen=True)
class GPSIndex:
    timestamps: list[float]
    latlng: list[tuple[float, float]]
    headings: list[float | None]
    imu_headings: list[float | None]


class GPSProcessor:
    def __init__(self, gps_data: list[dict[str, Any]]) -> None:
        self.gps_data = gps_data
        self.gps_index = self._build_gps_index(gps_data)
        self._median_coord: tuple[float, float] | None = None

    # ----- construction helpers -----

    @classmethod
    def from_json_file(cls, path: Path) -> GPSProcessor:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("GPS JSON must contain a list of timestamped samples")
        return cls(data)

    @staticmethod
    def _parse_heading(item: dict[str, Any]) -> float | None:
        for key in ("vehicle_heading_degrees", "heading_degrees", "heading"):
            if key not in item:
                continue
            value = item.get(key)
            if value is None:
                return None
            try:
                return float(value) % 360.0
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _parse_imu_heading(item: dict[str, Any]) -> float | None:
        """Extract heading from gyroscope yaw rate (gz) via integration."""
        gz = item.get("gyro_z")
        if gz is None:
            return None
        try:
            # gyroscope gz is yaw rate in rad/s
            # Approximate heading by integrating yaw rate from start
            return float(gz)
        except (TypeError, ValueError):
            return None

    def _build_gps_index(self, gps_data: list[dict[str, Any]]) -> GPSIndex:
        samples: list[tuple[float, float, float, float | None, float | None]] = []
        for item in gps_data:
            try:
                timestamp = float(item["timestamp_seconds"])
                lat = float(item["lat"])
                lng = float(item["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            heading = self._parse_heading(item)
            imu_heading = self._parse_imu_heading(item)
            samples.append((timestamp, lat, lng, heading, imu_heading))

        if not samples:
            raise ValueError(
                "GPS JSON must include timestamp_seconds, lat, and lng values"
            )

        samples.sort(key=lambda row: row[0])

        # Integrate gyroscope yaw rate to get absolute heading
        imu_headings = [row[4] for row in samples]
        if any(h is not None for h in imu_headings):
            integrated_heading: float | None = None
            for i in range(len(samples)):
                gz = samples[i][4]
                if gz is not None:
                    if integrated_heading is None:
                        integrated_heading = 0.0
                    if i > 0:
                        dt = samples[i][0] - samples[i - 1][0]
                        integrated_heading += gz * dt  # integrate yaw rate
                    imu_headings[i] = (integrated_heading * 180.0 / math.pi) % 360.0
                elif integrated_heading is not None:
                    imu_headings[i] = integrated_heading

        return GPSIndex(
            timestamps=[row[0] for row in samples],
            latlng=[(row[1], row[2]) for row in samples],
            headings=[row[3] for row in samples],
            imu_headings=imu_headings,
        )

    # ----- stationary ride detection -----

    def is_stationary(self) -> bool:
        return is_stationary_gps_track(self.gps_data)

    def median_coordinate(self) -> tuple[float, float]:
        if self._median_coord is not None:
            return self._median_coord
        lats = sorted(
            float(item["lat"]) for item in self.gps_data if "lat" in item
        )
        lngs = sorted(
            float(item["lng"]) for item in self.gps_data if "lng" in item
        )
        self._median_coord = lats[len(lats) // 2], lngs[len(lngs) // 2]
        return self._median_coord

    def collapse_to_median(self) -> list[dict[str, Any]]:
        median_lat, median_lng = self.median_coordinate()
        return [
            {
                "timestamp_seconds": item["timestamp_seconds"],
                "lat": median_lat,
                "lng": median_lng,
            }
            for item in self.gps_data
        ]

    # ----- GPS index lookups -----

    def _closest_index(self, current_time: float) -> int:
        pos = bisect_left(self.gps_index.timestamps, current_time)
        if pos <= 0:
            return 0
        if pos >= len(self.gps_index.timestamps):
            return len(self.gps_index.timestamps) - 1
        before = pos - 1
        after = pos
        if abs(self.gps_index.timestamps[before] - current_time) <= abs(
            self.gps_index.timestamps[after] - current_time
        ):
            return before
        return after

    def interpolate_sample(
        self, current_time: float
    ) -> tuple[int, float, float, float | None]:
        timestamps = self.gps_index.timestamps
        pos = bisect_left(timestamps, current_time)
        if pos <= 0:
            lat, lng = self.gps_index.latlng[0]
            return 0, lat, lng, self.gps_index.headings[0]
        if pos >= len(timestamps):
            last = len(timestamps) - 1
            lat, lng = self.gps_index.latlng[last]
            return last, lat, lng, self.gps_index.headings[last]

        t1 = timestamps[pos - 1]
        t2 = timestamps[pos]
        lat1, lng1 = self.gps_index.latlng[pos - 1]
        lat2, lng2 = self.gps_index.latlng[pos]
        if t2 == t1:
            return pos - 1, lat1, lng1, self.gps_index.headings[pos - 1]

        fraction = (current_time - t1) / (t2 - t1)
        lat = lat1 + fraction * (lat2 - lat1)
        lng = lng1 + fraction * (lng2 - lng1)

        heading1 = self.gps_index.headings[pos - 1]
        heading2 = self.gps_index.headings[pos]
        heading = None
        if heading1 is not None and heading2 is not None:
            heading = self._lerp_heading_degrees(heading1, heading2, fraction)

        # Use IMU heading if GPS heading unavailable
        if heading is None:
            imu1 = self.gps_index.imu_headings[pos - 1]
            imu2 = self.gps_index.imu_headings[pos]
            if imu1 is not None and imu2 is not None:
                heading = self._lerp_heading_degrees(imu1, imu2, fraction)
            elif imu1 is not None:
                heading = imu1
            elif imu2 is not None:
                heading = imu2

        if abs(current_time - t1) <= abs(t2 - current_time):
            idx = pos - 1
        else:
            idx = pos

        return idx, lat, lng, heading

    @staticmethod
    def _lerp_heading_degrees(
        start_deg: float, end_deg: float, fraction: float
    ) -> float:
        fraction = max(0.0, min(1.0, fraction))
        delta = (end_deg - start_deg + 180.0) % 360.0 - 180.0
        return (start_deg + fraction * delta + 360.0) % 360.0

    # ----- heading estimation -----

    def estimate_heading(self, idx: int) -> float | None:
        # Prefer IMU-integrated heading when available
        imu_heading = self.gps_index.imu_headings[idx]
        if imu_heading is not None:
            return imu_heading

        # Fall back to GPS-based bearing estimation
        sample_count = len(self.gps_index.latlng)
        if sample_count < 5:
            return None

        total_displacement = haversine_distance_meters(
            self.gps_index.latlng[0][0],
            self.gps_index.latlng[0][1],
            self.gps_index.latlng[-1][0],
            self.gps_index.latlng[-1][1],
        )
        if total_displacement < STATIONARY_THRESHOLD_METERS:
            return None

        if idx <= 0:
            start_idx, end_idx = 0, 1
        elif idx >= sample_count - 1:
            start_idx, end_idx = sample_count - 2, sample_count - 1
        else:
            start_idx, end_idx = idx - 1, idx + 1

        lat_a, lng_a = self.gps_index.latlng[start_idx]
        lat_b, lng_b = self.gps_index.latlng[end_idx]
        if (
            haversine_distance_meters(lat_a, lng_a, lat_b, lng_b)
            < STATIONARY_THRESHOLD_METERS
        ):
            return None

        return bearing_degrees(lat_a, lng_a, lat_b, lng_b)

    # ----- GPS projection -----

    @staticmethod
    def offset_lat_lng(
        base_lat: float,
        base_lng: float,
        dx_meters: float,
        dy_meters: float,
        bearing_deg: float,
    ) -> tuple[float, float]:
        bearing_rad = math.radians(bearing_deg)
        north_m = dy_meters * math.cos(bearing_rad) - dx_meters * math.sin(bearing_rad)
        east_m = dy_meters * math.sin(bearing_rad) + dx_meters * math.cos(bearing_rad)

        lat_rad = math.radians(base_lat)
        cos_lat = math.cos(lat_rad)
        if abs(cos_lat) < 1e-12:
            return base_lat, base_lng

        delta_lat = north_m / EARTH_RADIUS_METERS
        delta_lng = east_m / (EARTH_RADIUS_METERS * cos_lat)

        return (
            base_lat + math.degrees(delta_lat),
            base_lng + math.degrees(delta_lng),
        )

    def project_detection_to_gps(
        self,
        current_time: float,
        dx_meters: float,
        dy_meters: float,
    ) -> tuple[float, float]:
        base_lat, base_lng = interpolate_coordinate_at_time(
            self.gps_data, current_time
        )
        idx = self._closest_index(current_time)
        heading = self.estimate_heading(idx)
        if dx_meters == 0.0 and dy_meters == 0.0:
            return base_lat, base_lng
        if heading is None:
            return base_lat, base_lng
        return self.offset_lat_lng(base_lat, base_lng, dx_meters, dy_meters, heading)
