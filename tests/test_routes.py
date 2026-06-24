from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from processing.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_supabase():
    with patch("processing.services.supabase_client.get_supabase_service") as mock:
        svc = MagicMock()
        mock.return_value = svc
        yield svc


@pytest.fixture
def mock_blob():
    with patch("processing.services.blob_storage.get_blob_storage_service") as mock:
        svc = MagicMock()
        mock.return_value = svc
        yield svc


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data
        assert "uptime_seconds" in data
        assert data["version"] == "1.0.0"

    def test_health_detail_returns_200(self, client, mock_supabase):
        mock_supabase.select.return_value = [{"id": "test"}]
        with patch("processing.api.routes.health.get_supabase_service", return_value=mock_supabase):
            response = client.get("/health/detail")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["supabase"] == "connected"

    def test_health_detail_supabase_error(self, client, mock_supabase):
        mock_supabase.select.side_effect = Exception("Connection refused")
        with patch("processing.api.routes.health.get_supabase_service", return_value=mock_supabase):
            response = client.get("/health/detail")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert "error" in data["supabase"]


class TestRidesEndpoints:
    def test_list_rides_requires_auth(self, client):
        response = client.get("/rides")
        assert response.status_code == 401

    def test_list_rides_with_valid_token(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = [
            {"id": "ride-1", "user_id": "user-123", "status": "completed"}
        ]
        with patch("processing.api.routes.rides.get_supabase_service", return_value=mock_svc):
            response = client.get(
                "/rides",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 200
        data = response.json()
        assert "rides" in data
        assert len(data["rides"]) == 1

    def test_get_ride_requires_auth(self, client):
        response = client.get("/rides/ride-123")
        assert response.status_code == 401

    def test_get_ride_not_found(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = []
        with patch("processing.api.routes.rides.get_supabase_service", return_value=mock_svc):
            response = client.get(
                "/rides/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 404

    def test_get_ride_forbidden(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = [
            {"id": "ride-123", "user_id": "other-user", "status": "completed"}
        ]
        with patch("processing.api.routes.rides.get_supabase_service", return_value=mock_svc):
            response = client.get(
                "/rides/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 403

    def test_delete_ride_requires_auth(self, client):
        response = client.delete("/rides/ride-123")
        assert response.status_code == 401

    def test_delete_ride_not_found(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = []
        with patch("processing.api.routes.rides.get_supabase_service", return_value=mock_svc):
            response = client.delete(
                "/rides/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 404

    def test_delete_ride_forbidden(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = [
            {"user_id": "other-user"}
        ]
        with patch("processing.api.routes.rides.get_supabase_service", return_value=mock_svc):
            response = client.delete(
                "/rides/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 403


class TestUploadEndpoints:
    def test_init_upload_requires_auth(self, client):
        response = client.post(
            "/upload/init",
            json={"video_filename": "test.mp4", "gps_filename": "test.json"},
        )
        assert response.status_code == 401

    def test_init_upload_success(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_blob = MagicMock()
        mock_blob.generate_sas_url.return_value = (
            "https://example.com/sas?token=abc",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with patch("processing.upload_api.get_supabase_service", return_value=mock_svc), \
             patch("processing.upload_api.get_blob_storage_service", return_value=mock_blob):
            response = client.post(
                "/upload/init",
                json={"video_filename": "test.mp4", "gps_filename": "test.json"},
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 200
        data = response.json()
        assert "ride_id" in data
        assert "video_sas_url" in data
        assert "gps_sas_url" in data


class TestProcessEndpoints:
    def test_process_ride_requires_auth(self, client):
        response = client.post("/process/ride-123")
        assert response.status_code == 401

    def test_process_ride_not_found(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = []
        with patch("processing.api.routes.process.get_supabase_service", return_value=mock_svc):
            response = client.post(
                "/process/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 404

    def test_process_ride_forbidden(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = [
            {"id": "ride-123", "user_id": "other-user", "status": "queued"}
        ]
        with patch("processing.api.routes.process.get_supabase_service", return_value=mock_svc):
            response = client.post(
                "/process/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 403

    def test_process_ride_already_processing(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = [
            {"id": "ride-123", "user_id": "user-123", "status": "processing"}
        ]
        with patch("processing.api.routes.process.get_supabase_service", return_value=mock_svc):
            response = client.post(
                "/process/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 409

    def test_process_ride_already_completed(self, client):
        mock_svc = MagicMock()
        mock_svc.validate_token.return_value = {"user_id": "user-123"}
        mock_svc.select.return_value = [
            {"id": "ride-123", "user_id": "user-123", "status": "completed"}
        ]
        with patch("processing.api.routes.process.get_supabase_service", return_value=mock_svc):
            response = client.post(
                "/process/ride-123",
                headers={"Authorization": "Bearer valid-token"},
            )
        assert response.status_code == 409
