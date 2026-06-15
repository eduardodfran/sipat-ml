from __future__ import annotations

import math
from typing import Any

import numpy as np

EARTH_RADIUS_METERS = 6371008.8
STATIONARY_THRESHOLD_METERS = 5.0


def haversine_distance_meters(
    lat_a: float, lng_a: float, lat_b: float, lng_b: float
) -> float:
    lat_a_rad, lng_a_rad, lat_b_rad, lng_b_rad = np.radians(
        [lat_a, lng_a, lat_b, lng_b]
    )
    delta_lat = lat_b_rad - lat_a_rad
    delta_lng = lng_b_rad - lng_a_rad

    a = (
        np.sin(delta_lat / 2.0) ** 2
        + np.cos(lat_a_rad) * np.cos(lat_b_rad) * np.sin(delta_lng / 2.0) ** 2
    )
    return float(2.0 * EARTH_RADIUS_METERS * np.arcsin(np.sqrt(a)))


def bearing_degrees(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    lat_a_rad = math.radians(lat_a)
    lat_b_rad = math.radians(lat_b)
    delta_lng = math.radians(lng_b - lng_a)

    x = math.sin(delta_lng) * math.cos(lat_b_rad)
    y = math.cos(lat_a_rad) * math.sin(lat_b_rad) - math.sin(lat_a_rad) * math.cos(
        lat_b_rad
    ) * math.cos(delta_lng)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def is_stationary_gps_track(
    gps_data: list[dict[str, Any]],
    max_consecutive_distance: float = STATIONARY_THRESHOLD_METERS,
) -> bool:
    samples = [
        (float(item["lat"]), float(item["lng"]))
        for item in gps_data
        if "lat" in item and "lng" in item
    ]
    if len(samples) < 2:
        return True
    for i in range(1, len(samples)):
        d = haversine_distance_meters(
            samples[i - 1][0], samples[i - 1][1], samples[i][0], samples[i][1]
        )
        if d >= max_consecutive_distance:
            return False
    return True
