from __future__ import annotations

import pytest

from processing.core.clusterer import (
    PotholeClusterer,
    _aggregate_user_detections,
    _avg_confidence,
    _first_image_url,
    _max_frame_severity,
    _max_phys_area,
    _median_phys_area,
)


def _make_detection(lat: float, lng: float, **overrides) -> dict:
    base = {"lat": lat, "lng": lng, "phys_area_m2": 0.5, "image_url": None}
    base.update(overrides)
    return base


class TestPotholeClustererInit:
    def test_default_params(self):
        c = PotholeClusterer()
        assert c.max_distance_meters == 15.0
        assert c.min_detections == 3

    def test_custom_params(self):
        c = PotholeClusterer(max_distance_meters=10.0, min_detections=5)
        assert c.max_distance_meters == 10.0
        assert c.min_detections == 5


class TestPotholeClustererCluster:
    def test_empty_input(self):
        c = PotholeClusterer()
        assert c.cluster([]) == []

    def test_single_cluster_three_points(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810),
            _make_detection(14.55481, 121.04811),
            _make_detection(14.55482, 121.04812),
        ]
        result = c.cluster(data)
        assert len(result) == 1
        assert result[0]["detection_count"] == 3

    def test_two_separate_clusters(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810),
            _make_detection(14.55481, 121.04811),
            _make_detection(14.55482, 121.04812),
            _make_detection(14.56000, 121.05300),
            _make_detection(14.56001, 121.05301),
            _make_detection(14.56002, 121.05302),
        ]
        result = c.cluster(data)
        assert len(result) == 2

    def test_too_few_for_min_detections_returns_empty(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=5)
        data = [
            _make_detection(14.55480, 121.04810),
            _make_detection(14.55481, 121.04811),
            _make_detection(14.55482, 121.04812),
        ]
        assert c.cluster(data) == []

    def test_points_far_apart_no_cluster(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810),
            _make_detection(14.60000, 121.10000),
            _make_detection(14.70000, 121.20000),
        ]
        assert c.cluster(data) == []

    def test_cluster_lat_lng_is_average(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55000, 121.04000),
            _make_detection(14.55001, 121.04001),
            _make_detection(14.55002, 121.04002),
        ]
        result = c.cluster(data)
        assert len(result) == 1
        assert result[0]["lat"] == pytest.approx(14.55001, abs=1e-3)
        assert result[0]["lng"] == pytest.approx(121.04001, abs=1e-3)

    def test_cluster_max_area_is_max(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810, phys_area_m2=1.0),
            _make_detection(14.55481, 121.04811, phys_area_m2=5.0),
            _make_detection(14.55482, 121.04812, phys_area_m2=2.0),
        ]
        result = c.cluster(data)
        assert result[0]["max_area_m2"] == 5.0

    def test_cluster_median_area_is_median(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810, phys_area_m2=1.0),
            _make_detection(14.55481, 121.04811, phys_area_m2=5.0),
            _make_detection(14.55482, 121.04812, phys_area_m2=2.0),
        ]
        result = c.cluster(data)
        assert result[0]["median_area_m2"] == pytest.approx(2.0)

    def test_cluster_has_median_area_key(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810),
            _make_detection(14.55481, 121.04811),
            _make_detection(14.55482, 121.04812),
        ]
        result = c.cluster(data)
        assert "median_area_m2" in result[0]

    def test_cluster_worst_severity(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810, severity="Minor"),
            _make_detection(14.55481, 121.04811, severity="Severe"),
            _make_detection(14.55482, 121.04812, severity="Moderate"),
        ]
        result = c.cluster(data)
        assert result[0]["max_frame_severity"] == "Severe"

    def test_cluster_avg_confidence(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810, confidence=0.9),
            _make_detection(14.55481, 121.04811, confidence=0.5),
            _make_detection(14.55482, 121.04812, confidence=0.1),
        ]
        result = c.cluster(data)
        assert result[0]["avg_confidence"] == pytest.approx(0.5, abs=0.01)

    def test_cluster_user_detections_aggregated(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810, user_id="u1", video_timestamp=10.0),
            _make_detection(14.55481, 121.04811, user_id="u1", video_timestamp=5.0),
            _make_detection(14.55482, 121.04812, user_id="u2", video_timestamp=8.0),
        ]
        result = c.cluster(data)
        users = result[0]["user_detections"]
        assert len(users) == 2
        assert users[0]["user_id"] == "u1"
        assert users[0]["video_timestamp"] == 5.0
        assert users[1]["user_id"] == "u2"

    def test_image_url_first_non_null(self):
        c = PotholeClusterer(max_distance_meters=5.0, min_detections=3)
        data = [
            _make_detection(14.55480, 121.04810, image_url=None),
            _make_detection(14.55481, 121.04811, image_url="https://example.com/img.jpg"),
            _make_detection(14.55482, 121.04812, image_url=None),
        ]
        result = c.cluster(data)
        assert result[0]["image_url"] == "https://example.com/img.jpg"


class TestFirstImageUrl:
    def test_with_url(self):
        import pandas as pd
        df = pd.DataFrame({"image_url": ["https://example.com/a.jpg", None]})
        assert _first_image_url(df) == "https://example.com/a.jpg"

    def test_without_column(self):
        import pandas as pd
        df = pd.DataFrame({"other": [1]})
        assert _first_image_url(df) is None

    def test_all_null(self):
        import pandas as pd
        df = pd.DataFrame({"image_url": [None, None]})
        assert _first_image_url(df) is None

    def test_empty_df(self):
        import pandas as pd
        df = pd.DataFrame({"image_url": []})
        assert _first_image_url(df) is None


class TestMaxFrameSeverity:
    def test_picks_worst(self):
        import pandas as pd
        df = pd.DataFrame({"severity": ["Minor", "Severe", "Moderate"]})
        assert _max_frame_severity(df) == "Severe"

    def test_without_severity_column(self):
        import pandas as pd
        df = pd.DataFrame({"other": [1]})
        assert _max_frame_severity(df) == "Minor"

    def test_single_row(self):
        import pandas as pd
        df = pd.DataFrame({"severity": ["Moderate"]})
        assert _max_frame_severity(df) == "Moderate"


class TestAggregateUserDetections:
    def test_basic_aggregation(self):
        import pandas as pd
        df = pd.DataFrame({
            "user_id": ["u1", "u1", "u2"],
            "video_timestamp": [10.0, 5.0, 8.0],
        })
        result = _aggregate_user_detections(df)
        assert len(result) == 2
        assert result[0]["user_id"] == "u1"
        assert result[0]["video_timestamp"] == 5.0

    def test_without_user_id_column(self):
        import pandas as pd
        df = pd.DataFrame({"other": [1]})
        assert _aggregate_user_detections(df) == []

    def test_null_user_id_skipped(self):
        import pandas as pd
        df = pd.DataFrame({
            "user_id": [None, "u1"],
            "video_timestamp": [5.0, 10.0],
        })
        result = _aggregate_user_detections(df)
        assert len(result) == 1

    def test_null_timestamp_not_overwriting_valid(self):
        import pandas as pd
        df = pd.DataFrame({
            "user_id": ["u1", "u1"],
            "video_timestamp": [10.0, 5.0],
        })
        result = _aggregate_user_detections(df)
        assert len(result) == 1
        assert result[0]["video_timestamp"] == 5.0


class TestMaxPhysArea:
    def test_with_ride_id_grouping(self):
        import pandas as pd
        df = pd.DataFrame({
            "phys_area_m2": [1.0, 5.0, 2.0, 3.0],
            "ride_id": ["a", "a", "b", "b"],
        })
        assert _max_phys_area(df) == pytest.approx(5.0)

    def test_without_ride_id(self):
        import pandas as pd
        df = pd.DataFrame({"phys_area_m2": [1.0, 5.0, 2.0]})
        assert _max_phys_area(df) == pytest.approx(5.0)

    def test_without_phys_area_column(self):
        import pandas as pd
        df = pd.DataFrame({"other": [1]})
        assert _max_phys_area(df) == 0.0


class TestMedianPhysArea:
    def test_without_ride_id(self):
        import pandas as pd
        df = pd.DataFrame({"phys_area_m2": [1.0, 5.0, 2.0]})
        assert _median_phys_area(df) == pytest.approx(2.0)

    def test_with_ride_id_grouping(self):
        import pandas as pd
        df = pd.DataFrame({
            "phys_area_m2": [1.0, 5.0, 2.0, 8.0],
            "ride_id": ["a", "a", "b", "b"],
        })
        # per-ride medians: a->3.0, b->5.0; overall median of [3.0, 5.0] = 4.0
        assert _median_phys_area(df) == pytest.approx(4.0)

    def test_without_phys_area_column(self):
        import pandas as pd
        df = pd.DataFrame({"other": [1]})
        assert _median_phys_area(df) == 0.0

    def test_all_zero_areas_returns_zero(self):
        import pandas as pd
        df = pd.DataFrame({"phys_area_m2": [0.0, 0.0, 0.0]})
        assert _median_phys_area(df) == 0.0

    def test_single_value(self):
        import pandas as pd
        df = pd.DataFrame({"phys_area_m2": [3.14]})
        assert _median_phys_area(df) == pytest.approx(3.14)

    def test_ignores_zero_areas_in_median(self):
        import pandas as pd
        df = pd.DataFrame({"phys_area_m2": [0.0, 0.0, 2.0, 4.0]})
        # zeros filtered out, median of [2.0, 4.0] = 3.0
        assert _median_phys_area(df) == pytest.approx(3.0)


class TestAvgConfidence:
    def test_with_confidence(self):
        import pandas as pd
        df = pd.DataFrame({"confidence": [0.9, 0.5, 0.1]})
        assert _avg_confidence(df) == pytest.approx(0.5)

    def test_without_column(self):
        import pandas as pd
        df = pd.DataFrame({"other": [1]})
        assert _avg_confidence(df) == 0.0

    def test_empty_column(self):
        import pandas as pd
        df = pd.DataFrame({"confidence": []})
        assert _avg_confidence(df) == 0.0
