from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy ML dependencies that aren't installed in CI/local dev
if "ultralytics" not in sys.modules:
    sys.modules["ultralytics"] = MagicMock()
if "cv2" not in sys.modules:
    sys.modules["cv2"] = MagicMock()

from processing.pipeline.worker import RideProcessor
from processing.services.blob_storage import BlobStorageService
from processing.services.supabase_client import SupabaseService


class TestRideProcessorInit:
    @patch("processing.pipeline.worker.get_blob_storage_service")
    @patch("processing.pipeline.worker.get_supabase_service")
    def test_init_with_defaults(self, mock_get_supabase, mock_get_blob):
        mock_get_supabase.return_value = MagicMock()
        mock_get_blob.return_value = MagicMock()
        rp = RideProcessor()
        assert rp._svc is not None
        assert rp._blob is not None
        assert rp._model is None

    def test_init_with_injected_services(self):
        mock_svc = MagicMock(spec=SupabaseService)
        mock_blob = MagicMock(spec=BlobStorageService)
        rp = RideProcessor(supabase=mock_svc, blob=mock_blob)
        assert rp._svc is mock_svc
        assert rp._blob is mock_blob

    def test_supabase_property_returns_client(self):
        mock_svc = MagicMock(spec=SupabaseService)
        mock_client = MagicMock()
        mock_svc.client = mock_client
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        assert rp._supabase is mock_client


class TestResolveVideoPath:
    def test_video_bucket_path(self):
        row = {"video_bucket_path": "videos/ride1.mp4"}
        assert RideProcessor._resolve_video_path(row) == "videos/ride1.mp4"

    def test_video_path(self):
        row = {"video_path": "path/to/video.mp4"}
        assert RideProcessor._resolve_video_path(row) == "path/to/video.mp4"

    def test_video_uri(self):
        row = {"video_uri": "https://example.com/video.mp4"}
        assert RideProcessor._resolve_video_path(row) == "https://example.com/video.mp4"

    def test_file_name_fallback(self):
        row = {"file_name": "test.mp4"}
        assert RideProcessor._resolve_video_path(row) == "test.mp4"

    def test_no_video_path_raises(self):
        row = {"other_field": "value"}
        with pytest.raises(KeyError, match="Could not determine video path"):
            RideProcessor._resolve_video_path(row)

    def test_empty_string_falls_through(self):
        row = {"video_path": "  "}
        with pytest.raises(KeyError):
            RideProcessor._resolve_video_path(row)


class TestResolveGpsPath:
    def test_gps_bucket_path(self):
        row = {"gps_bucket_path": "gps/ride1.json"}
        assert RideProcessor._resolve_gps_path(row, "video.mp4") == "gps/ride1.json"

    def test_gps_json_path(self):
        row = {"gps_json_path": "gps/data.json"}
        assert RideProcessor._resolve_gps_path(row, "video.mp4") == "gps/data.json"

    def test_csv_path_converted_to_json(self):
        row = {"csv_path": "gps/data.csv"}
        result = RideProcessor._resolve_gps_path(row, "video.mp4")
        assert result.endswith("data.json")

    def test_no_gps_path_infers_from_video(self):
        row = {}
        result = RideProcessor._resolve_gps_path(row, "videos/ride1.mp4")
        assert result.endswith("ride1.json")


class TestFirstPresentValue:
    def test_first_key_found(self):
        row = {"a": "val_a", "b": "val_b"}
        assert RideProcessor._first_present_value(row, ["a", "b"]) == "val_a"

    def test_second_key_found(self):
        row = {"b": "val_b"}
        assert RideProcessor._first_present_value(row, ["a", "b"]) == "val_b"

    def test_no_keys_found(self):
        row = {"c": "val_c"}
        assert RideProcessor._first_present_value(row, ["a", "b"]) is None

    def test_empty_string_ignored(self):
        row = {"a": "", "b": "val_b"}
        assert RideProcessor._first_present_value(row, ["a", "b"]) == "val_b"

    def test_whitespace_only_ignored(self):
        row = {"a": "  ", "b": "val_b"}
        assert RideProcessor._first_present_value(row, ["a", "b"]) == "val_b"

    def test_non_string_value_ignored(self):
        row = {"a": 123, "b": "val_b"}
        assert RideProcessor._first_present_value(row, ["a", "b"]) == "val_b"


class TestFindMatchingPothole:
    def test_exact_match(self):
        potholes = [
            {"id": "p1", "consolidated_latitude": 14.55480, "consolidated_longitude": 121.04810},
        ]
        match = RideProcessor._find_matching_pothole(potholes, 14.55480, 121.04810)
        assert match is not None
        assert match["id"] == "p1"

    def test_no_match_too_far(self):
        potholes = [
            {"id": "p1", "consolidated_latitude": 14.55480, "consolidated_longitude": 121.04810},
        ]
        match = RideProcessor._find_matching_pothole(potholes, 14.60000, 121.10000)
        assert match is None

    def test_match_within_radius(self):
        potholes = [
            {"id": "p1", "consolidated_latitude": 14.55480, "consolidated_longitude": 121.04810},
        ]
        match = RideProcessor._find_matching_pothole(potholes, 14.55481, 121.04811, radius=20.0)
        assert match is not None

    def test_pothole_with_none_coords_skipped(self):
        potholes = [
            {"id": "p1", "consolidated_latitude": None, "consolidated_longitude": None},
        ]
        match = RideProcessor._find_matching_pothole(potholes, 14.55480, 121.04810)
        assert match is None

    def test_empty_potholes(self):
        match = RideProcessor._find_matching_pothole([], 14.55480, 121.04810)
        assert match is None

    def test_returns_closest_within_radius(self):
        potholes = [
            {"id": "p1", "consolidated_latitude": 14.55480, "consolidated_longitude": 121.04810},
            {"id": "p2", "consolidated_latitude": 14.55490, "consolidated_longitude": 121.04820},
        ]
        match = RideProcessor._find_matching_pothole(potholes, 14.55485, 121.04815)
        assert match is not None
        assert match["id"] == "p2"


class TestMergeUserDetections:
    def test_merges_unique_users(self):
        existing = [{"user_id": "u1", "video_timestamp": 10.0}]
        incoming = [{"user_id": "u2", "video_timestamp": 5.0}]
        result = RideProcessor._merge_user_detections(existing, incoming)
        assert len(result) == 2

    def test_keeps_earlier_timestamp(self):
        existing = [{"user_id": "u1", "video_timestamp": 10.0}]
        incoming = [{"user_id": "u1", "video_timestamp": 5.0}]
        result = RideProcessor._merge_user_detections(existing, incoming)
        assert len(result) == 1
        assert result[0]["video_timestamp"] == 5.0

    def test_sorted_by_timestamp(self):
        existing = [{"user_id": "u2", "video_timestamp": 20.0}]
        incoming = [{"user_id": "u1", "video_timestamp": 5.0}]
        result = RideProcessor._merge_user_detections(existing, incoming)
        assert result[0]["user_id"] == "u1"
        assert result[1]["user_id"] == "u2"

    def test_ignores_missing_user_id(self):
        existing = [{"user_id": None, "video_timestamp": 10.0}]
        incoming = [{"user_id": "u1", "video_timestamp": 5.0}]
        result = RideProcessor._merge_user_detections(existing, incoming)
        assert len(result) == 1

    def test_ignores_missing_timestamp(self):
        existing = [{"user_id": "u1", "video_timestamp": None}]
        incoming = [{"user_id": "u1", "video_timestamp": 5.0}]
        result = RideProcessor._merge_user_detections(existing, incoming)
        assert len(result) == 1
        assert result[0]["video_timestamp"] == 5.0

    def test_empty_inputs(self):
        result = RideProcessor._merge_user_detections([], [])
        assert result == []


class TestFriendlyError:
    def test_generic_exception(self):
        msg = RideProcessor._friendly_error(RuntimeError("bad stuff"), "testing")
        assert "bad stuff" in msg

    def test_api_error_with_message(self):
        exc = Exception({"message": "duplicate key", "hint": "check id", "code": "23505"})
        msg = RideProcessor._friendly_error(exc, "inserting")
        assert "duplicate key" in msg
        assert "check id" in msg
        assert "23505" in msg


class TestMarkFailedAndCompleted:
    def test_mark_failed(self):
        mock_svc = MagicMock()
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        rp._mark_failed("ride-123", "something broke")
        mock_svc.update.assert_called_once_with(
            "rides_metadata",
            {"status": "failed", "error_log": "something broke"},
            id="ride-123",
        )

    def test_mark_completed(self):
        mock_svc = MagicMock()
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        rp._mark_completed("ride-123")
        mock_svc.update.assert_called_once_with(
            "rides_metadata",
            {"status": "completed"},
            id="ride-123",
        )


class TestClaimOldestQueuedRide:
    def test_returns_none_when_no_queued(self):
        mock_svc = MagicMock()
        mock_svc.client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = []
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        assert rp.claim_oldest_queued_ride() is None

    def test_claims_and_updates_status(self):
        mock_svc = MagicMock()
        execute_result = MagicMock()
        execute_result.data = [{"id": "ride-1", "status": "queued"}]
        mock_svc.client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = execute_result
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        result = rp.claim_oldest_queued_ride()
        assert result is not None
        assert result["status"] == "processing"
        mock_svc.update.assert_called_once_with(
            "rides_metadata", {"status": "processing"}, id="ride-1"
        )

    def test_raises_on_missing_id(self):
        mock_svc = MagicMock()
        execute_result = MagicMock()
        execute_result.data = [{"status": "queued"}]
        mock_svc.client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = execute_result
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        with pytest.raises(KeyError, match="no id"):
            rp.claim_oldest_queued_ride()


class TestProcessRideErrorHandling:
    def test_video_download_failure_marks_failed(self):
        mock_svc = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_file.side_effect = OSError("download failed")
        rp = RideProcessor(supabase=mock_svc, blob=mock_blob)
        ride = {"id": "ride-err", "user_id": "u1", "video_bucket_path": "vid.mp4"}
        with pytest.raises(RuntimeError):
            rp.process_next_queued()

    def test_process_ride_no_id_raises(self):
        rp = RideProcessor(supabase=MagicMock(), blob=MagicMock())
        with pytest.raises(KeyError, match="no id"):
            rp.process_ride({})

    @patch("processing.pipeline.worker.GPSProcessor")
    @patch("processing.pipeline.worker.DetectionBatchBuilder")
    def test_process_ride_calls_mark_completed(
        self, MockBuilder, MockGPS
    ):
        mock_svc = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_file.return_value = b"fake-video-bytes"
        rp = RideProcessor(supabase=mock_svc, blob=mock_blob)

        mock_gps = MagicMock()
        MockGPS.from_json_file.return_value = mock_gps

        mock_builder = MagicMock()
        mock_builder.build.return_value = [
            {"ride_id": "ride-1", "lat": 14.5, "lng": 121.0}
        ]
        MockBuilder.return_value = mock_builder

        ride = {
            "id": "ride-1",
            "user_id": "u1",
            "video_bucket_path": "vid.mp4",
            "gps_bucket_path": "gps.json",
        }

        with patch.object(rp, "_repair_video", side_effect=lambda p: p), \
             patch.object(rp, "_sync_potholes", return_value=0):
            result = rp.process_ride(ride)

        assert result["ride_id"] == "ride-1"
        assert result["raw_detection_count"] == 1
        mock_svc.update.assert_called_with(
            "rides_metadata", {"status": "completed"}, id="ride-1"
        )

    @patch("processing.pipeline.worker.GPSProcessor")
    @patch("processing.pipeline.worker.DetectionBatchBuilder")
    def test_process_ride_detection_failure_marks_failed(
        self, MockBuilder, MockGPS
    ):
        mock_svc = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_file.return_value = b"fake-video-bytes"
        rp = RideProcessor(supabase=mock_svc, blob=mock_blob)

        MockGPS.from_json_file.return_value = MagicMock()
        MockBuilder.return_value.build.side_effect = RuntimeError("YOLO crashed")

        ride = {
            "id": "ride-2",
            "user_id": "u1",
            "video_bucket_path": "vid.mp4",
            "gps_bucket_path": "gps.json",
        }

        with patch.object(rp, "_repair_video", side_effect=lambda p: p):
            with pytest.raises(RuntimeError):
                rp.process_ride(ride)


class TestSyncPotholes:
    def test_no_clusters_returns_zero(self):
        mock_svc = MagicMock()
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        with patch("processing.pipeline.worker.PotholeClusterer") as MockClusterer:
            MockClusterer.return_value.cluster.return_value = []
            result = rp._sync_potholes([], "ride-1")
            assert result == 0

    def test_new_pothole_inserted(self):
        mock_svc = MagicMock()
        mock_svc.select.return_value = []
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        cluster_result = [{
            "lat": 14.55480,
            "lng": 121.04810,
            "detection_count": 3,
            "max_area_m2": 0.5,
            "max_frame_severity": "Minor",
            "avg_confidence": 0.7,
            "user_detections": [{"user_id": "u1", "video_timestamp": 5.0}],
        }]
        with patch("processing.pipeline.worker.PotholeClusterer") as MockClusterer:
            MockClusterer.return_value.cluster.return_value = cluster_result
            count = rp._sync_potholes(cluster_result, "ride-1")
            assert count == 1
            rp._svc.client.schema.return_value.from_.return_value.insert.assert_called_once()

    def test_existing_pothole_updated(self):
        mock_svc = MagicMock()
        existing = [{
            "id": "existing-1",
            "consolidated_latitude": 14.55480,
            "consolidated_longitude": 121.04810,
            "total_detection_hits": 3,
            "worst_severity": "Minor",
            "user_detections": [],
        }]
        mock_svc.select.return_value = existing
        rp = RideProcessor(supabase=mock_svc, blob=MagicMock())
        cluster_result = [{
            "lat": 14.55480,
            "lng": 121.04810,
            "detection_count": 2,
            "max_area_m2": 0.5,
            "max_frame_severity": "Moderate",
            "avg_confidence": 0.6,
            "user_detections": [{"user_id": "u1", "video_timestamp": 5.0}],
        }]
        with patch("processing.pipeline.worker.PotholeClusterer") as MockClusterer:
            MockClusterer.return_value.cluster.return_value = cluster_result
            count = rp._sync_potholes(cluster_result, "ride-1")
            assert count == 1
            rp._svc.client.schema.return_value.from_.return_value.update.assert_called_once()
