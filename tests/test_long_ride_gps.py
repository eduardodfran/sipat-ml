from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from utils.gps_processor import GPSProcessor


def _make_segment(start_ts: float, count: int, lat0: float, lng0: float, step_deg: float = 0.0001) -> list[dict]:
    return [
        {"timestamp_seconds": start_ts + i * 2.0, "lat": lat0 + i * step_deg, "lng": lng0 + i * step_deg * 0.8, "gyro_z": 0.01}
        for i in range(count)
    ]


def test_15min_segmented_track_spans_full_extent():
    seg1 = _make_segment(0, 150, 14.5547, 121.0509)
    seg2 = _make_segment(300, 150, seg1[-1]["lat"], seg1[-1]["lng"])
    seg3 = _make_segment(600, 150, seg2[-1]["lat"], seg2[-1]["lng"])
    full = seg1 + seg2 + seg3
    proc = GPSProcessor(full)
    assert len(proc.gps_index.timestamps) == 450
    _, lat_start, lng_start, _ = proc.interpolate_sample(0)
    _, lat_mid, lng_mid, _ = proc.interpolate_sample(450)
    _, lat_end, lng_end, _ = proc.interpolate_sample(898)
    assert lat_start < lat_mid < lat_end
    assert lng_start < lng_mid < lng_end


def test_long_ride_not_clamped_to_start():
    gps_data = _make_segment(0, 150, 14.5547, 121.0509)
    proc = GPSProcessor(gps_data)
    _, lat_early, lng_early, _ = proc.interpolate_sample(10)
    _, lat_late, lng_late, _ = proc.interpolate_sample(290)
    assert abs(lat_early - lat_late) > 0.005
    assert abs(lng_early - lng_late) > 0.005


def test_epoch_ms_normalized_to_relative():
    first = 1_700_000_000_000
    raw = [
        {"timestamp_seconds": first, "lat": 14.5, "lng": 121.0},
        {"timestamp_seconds": first + 2000, "lat": 14.51, "lng": 121.01},
        {"timestamp_seconds": first + 4000, "lat": 14.52, "lng": 121.02},
    ]
    normalized = [
        {**r, "timestamp_seconds": (r["timestamp_seconds"] - first) / 1000.0} for r in raw
    ]
    proc = GPSProcessor(normalized)
    assert proc.gps_index.timestamps[0] == 0.0
    assert proc.gps_index.timestamps[-1] == 4.0


def test_from_json_file_15min():
    gps_data = _make_segment(0, 450, 14.5547, 121.0509)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump(gps_data, f)
        path = Path(f.name)
    try:
        proc = GPSProcessor.from_json_file(path)
        assert len(proc.gps_index.timestamps) == 450
        _, lat, lng, _ = proc.interpolate_sample(450)
        assert 14.55 < lat < 14.65
    finally:
        path.unlink(missing_ok=True)
