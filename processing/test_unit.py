from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.damage_severity import calculate_severity
from utils.geo_math import (
    bearing_degrees,
    haversine_distance_meters,
    is_stationary_gps_track,
)

try:
    from clustering import cluster_pothole_detections
    from clustering import (
        _avg_confidence,
        _max_phys_area_across_rides,
    )
except ImportError:
    cluster_pothole_detections = None
    _avg_confidence = None
    _max_phys_area_across_rides = None


# ----- calculate_severity -----

class TestCalculateSeverity:
    def test_minor_below_2_percent(self):
        assert calculate_severity([0.1, 0.1, 0.15, 0.15]) == "Minor"

    def test_minor_small_box(self):
        assert calculate_severity([0.4, 0.4, 0.5, 0.5]) == "Minor"

    def test_moderate_at_2_percent(self):
        assert calculate_severity([0.0, 0.0, 0.2, 0.1]) == "Moderate"

    def test_moderate_mid_range(self):
        assert calculate_severity([0.1, 0.1, 0.3, 0.25]) == "Moderate"

    def test_moderate_at_6_percent(self):
        assert calculate_severity([0.0, 0.0, 0.3, 0.2]) == "Moderate"

    def test_severe_above_6_percent(self):
        assert calculate_severity([0.0, 0.0, 0.5, 0.5]) == "Severe"

    def test_severe_full_frame(self):
        assert calculate_severity([0.0, 0.0, 1.0, 1.0]) == "Severe"

    def test_severe_large_box(self):
        assert calculate_severity([0.1, 0.1, 0.9, 0.9]) == "Severe"

    def test_zero_area_box(self):
        assert calculate_severity([0.5, 0.5, 0.5, 0.5]) == "Minor"

    def test_floating_point_boundary_moderate(self):
        bbox = [0.1, 0.1, 0.3, 0.2]
        area_pct = round(((0.3 - 0.1) * (0.2 - 0.1)) * 100, 2)
        assert area_pct == 2.0
        assert calculate_severity(bbox) == "Moderate"

    def test_floating_point_boundary_severe(self):
        bbox = [0.0, 0.0, 0.3, 0.2]
        area_pct = round(((0.3 - 0.0) * (0.2 - 0.0)) * 100, 2)
        assert area_pct == 6.0
        assert calculate_severity(bbox) == "Moderate"

    def test_none_bbox(self):
        with pytest.raises(TypeError):
            calculate_severity(None)


# ----- clustering -----

class TestClusterPotholeDetections:
    def test_empty_input_returns_empty_list(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        assert cluster_pothole_detections([]) == []

    def test_single_cluster_of_three(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        data = [
            {"lat": 14.55480, "lng": 121.04810, "phys_area_m2": 0.5, "image_url": None},
            {"lat": 14.55481, "lng": 121.04811, "phys_area_m2": 0.6, "image_url": None},
            {"lat": 14.55482, "lng": 121.04812, "phys_area_m2": 0.4, "image_url": None},
        ]
        result = cluster_pothole_detections(data, max_distance_meters=3.0, min_detections=3)
        assert len(result) == 1
        assert result[0]["detection_count"] == 3

    def test_two_separate_clusters(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        data = [
            {"lat": 14.55480, "lng": 121.04810, "phys_area_m2": 1.0, "image_url": None},
            {"lat": 14.55481, "lng": 121.04811, "phys_area_m2": 1.0, "image_url": None},
            {"lat": 14.55482, "lng": 121.04812, "phys_area_m2": 1.0, "image_url": None},
            {"lat": 14.56000, "lng": 121.05300, "phys_area_m2": 1.0, "image_url": None},
            {"lat": 14.56001, "lng": 121.05301, "phys_area_m2": 1.0, "image_url": None},
            {"lat": 14.56002, "lng": 121.05302, "phys_area_m2": 1.0, "image_url": None},
        ]
        result = cluster_pothole_detections(data, max_distance_meters=3.0, min_detections=3)
        assert len(result) == 2
        hits = sorted(p["detection_count"] for p in result)
        assert hits == [3, 3]

    def test_fewer_than_min_detections_returns_empty(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        data = [
            {"lat": 14.55480, "lng": 121.04810, "phys_area_m2": 1.0, "image_url": None},
            {"lat": 14.55481, "lng": 121.04811, "phys_area_m2": 1.0, "image_url": None},
        ]
        result = cluster_pothole_detections(data, max_distance_meters=3.0, min_detections=3)
        assert result == []

    def test_image_url_propagates_to_cluster(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        data = [
            {"lat": 14.55480, "lng": 121.04810, "phys_area_m2": 0.5, "image_url": "https://example.com/frame1.jpg"},
            {"lat": 14.55481, "lng": 121.04811, "phys_area_m2": 0.6, "image_url": None},
            {"lat": 14.55482, "lng": 121.04812, "phys_area_m2": 0.4, "image_url": None},
        ]
        result = cluster_pothole_detections(data, max_distance_meters=3.0, min_detections=3)
        assert len(result) == 1
        assert result[0]["image_url"] == "https://example.com/frame1.jpg"

    def test_phys_area_m2_max_computed(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        data = [
            {"lat": 14.55480, "lng": 121.04810, "phys_area_m2": 1.2, "image_url": None},
            {"lat": 14.55481, "lng": 121.04811, "phys_area_m2": 3.5, "image_url": None},
            {"lat": 14.55482, "lng": 121.04812, "phys_area_m2": 2.1, "image_url": None},
        ]
        result = cluster_pothole_detections(data, max_distance_meters=3.0, min_detections=3)
        assert result[0]["max_area_m2"] == 3.5

    def test_cluster_includes_avg_confidence(self):
        if cluster_pothole_detections is None:
            pytest.skip("clustering module not importable")
        data = [
            {"lat": 14.55480, "lng": 121.04810, "phys_area_m2": 0.5, "image_url": None, "confidence": 0.8},
            {"lat": 14.55481, "lng": 121.04811, "phys_area_m2": 0.6, "image_url": None, "confidence": 0.6},
            {"lat": 14.55482, "lng": 121.04812, "phys_area_m2": 0.4, "image_url": None, "confidence": 0.4},
        ]
        result = cluster_pothole_detections(data, max_distance_meters=3.0, min_detections=3)
        assert len(result) == 1
        assert "avg_confidence" in result[0]
        assert result[0]["avg_confidence"] == pytest.approx(0.6, abs=0.01)


# ----- clustering helpers -----

class TestClusteringHelpers:
    def test_avg_confidence_with_column(self):
        if _avg_confidence is None:
            pytest.skip("clustering module not importable")
        import pandas as pd
        df = pd.DataFrame({"confidence": [0.9, 0.5, 0.1]})
        assert _avg_confidence(df) == pytest.approx(0.5)

    def test_avg_confidence_without_column(self):
        if _avg_confidence is None:
            pytest.skip("clustering module not importable")
        import pandas as pd
        df = pd.DataFrame({"other": [1, 2]})
        assert _avg_confidence(df) == 0.0

    def test_avg_confidence_empty(self):
        if _avg_confidence is None:
            pytest.skip("clustering module not importable")
        import pandas as pd
        df = pd.DataFrame({"confidence": []})
        assert _avg_confidence(df) == 0.0

    def test_max_phys_area_with_ride_id(self):
        if _max_phys_area_across_rides is None:
            pytest.skip("clustering module not importable")
        import pandas as pd
        df = pd.DataFrame({
            "phys_area_m2": [1.0, 5.0, 2.0, 3.0],
            "ride_id": ["a", "a", "b", "b"],
        })
        assert _max_phys_area_across_rides(df) == pytest.approx(5.0)

    def test_max_phys_area_without_ride_id(self):
        if _max_phys_area_across_rides is None:
            pytest.skip("clustering module not importable")
        import pandas as pd
        df = pd.DataFrame({"phys_area_m2": [1.0, 5.0, 2.0]})
        assert _max_phys_area_across_rides(df) == pytest.approx(5.0)

    def test_max_phys_area_without_column(self):
        if _max_phys_area_across_rides is None:
            pytest.skip("clustering module not importable")
        import pandas as pd
        df = pd.DataFrame({"other": [1, 2]})
        assert _max_phys_area_across_rides(df) == 0.0


# ----- math helpers (batch_worker) -----

class TestHaversineDistance:
    def test_same_point_returns_zero(self):
        assert haversine_distance_meters(14.5, 121.0, 14.5, 121.0) == 0.0

    def test_known_distance(self):
        d = haversine_distance_meters(14.5547, 121.0509, 14.5548, 121.0510)
        assert 10 < d < 20

    def test_one_degree_lat_approx_111km(self):
        d = haversine_distance_meters(0.0, 0.0, 1.0, 0.0)
        assert 110000 < d < 112000

    def test_symmetric(self):
        d1 = haversine_distance_meters(14.5, 121.0, 14.6, 121.1)
        d2 = haversine_distance_meters(14.6, 121.1, 14.5, 121.0)
        assert abs(d1 - d2) < 1e-6


class TestBearingDegrees:
    def test_north_bearing(self):
        assert bearing_degrees(0.0, 0.0, 1.0, 0.0) == 0.0

    def test_east_bearing(self):
        assert bearing_degrees(0.0, 0.0, 0.0, 1.0) == 90.0

    def test_south_bearing(self):
        assert bearing_degrees(1.0, 0.0, 0.0, 0.0) == 180.0

    def test_west_bearing(self):
        assert bearing_degrees(0.0, 0.0, 0.0, -1.0) == 270.0

    def test_northeast_bearing(self):
        b = bearing_degrees(0.0, 0.0, 1.0, 1.0)
        assert 44 < b < 46

    def test_wraps_to_0_to_360(self):
        b = bearing_degrees(0.0, 0.0, -1.0, 0.0)
        assert b == 180.0


class TestIsStationaryGpsTrack:
    def test_single_point_is_stationary(self):
        data = [{"lat": 14.5, "lng": 121.0}]
        assert is_stationary_gps_track(data) is True

    def test_two_identical_points_is_stationary(self):
        data = [
            {"lat": 14.5, "lng": 121.0},
            {"lat": 14.5, "lng": 121.0},
        ]
        assert is_stationary_gps_track(data) is True

    def test_small_gps_drift_is_stationary(self):
        data = [
            {"lat": 14.5, "lng": 121.0},
            {"lat": 14.50001, "lng": 121.00001},
            {"lat": 14.50002, "lng": 121.00002},
        ]
        assert is_stationary_gps_track(data, max_consecutive_distance=5.0) is True

    def test_moving_track_not_stationary(self):
        data = [
            {"lat": 14.5, "lng": 121.0},
            {"lat": 14.51, "lng": 121.01},
            {"lat": 14.52, "lng": 121.02},
        ]
        assert is_stationary_gps_track(data, max_consecutive_distance=5.0) is False
