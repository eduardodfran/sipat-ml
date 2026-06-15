from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.geo_sync import interpolate_coordinate_at_time


def test_interpolate_returns_midpoint():
    gps_data = [
        {"timestamp_seconds": 4.0, "lat": 14.5240, "lng": 121.0480},
        {"timestamp_seconds": 5.0, "lat": 14.5250, "lng": 121.0490},
    ]

    lat, lng = interpolate_coordinate_at_time(gps_data, 4.5)

    assert lat == 14.5245
    assert lng == 121.0485


def test_interpolate_clamps_past_final_tick():
    gps_data = [
        {"timestamp_seconds": 4.0, "lat": 14.5240, "lng": 121.0480},
        {"timestamp_seconds": 5.0, "lat": 14.5250, "lng": 121.0490},
    ]

    lat, lng = interpolate_coordinate_at_time(gps_data, 5.03)

    assert lat == 14.5250
    assert lng == 121.0490
