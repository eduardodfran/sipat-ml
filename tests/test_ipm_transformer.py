from __future__ import annotations

import math
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

# Mock cv2 if not installed (CI / lightweight dev environments)
_cv2_available = False
try:
    import cv2 as _cv2_real
    _cv2_available = True
except ImportError:
    sys.modules["cv2"] = MagicMock()

from processing.utils.camera_calibration import CameraCalibration
from processing.utils.ipm_transformer import (
    DEFAULT_FAR_METERS,
    DEFAULT_NEAR_METERS,
    DISTANCE_CORRECTION_ALPHA,
    IPMTransformer,
)


def _default_calibration(**overrides) -> CameraCalibration:
    defaults = dict(
        fx=1200.0,
        fy=1200.0,
        cx=960.0,
        cy=540.0,
        height_m=1.3,
        pitch_deg=6.0,
        roll_deg=0.0,
        yaw_deg=0.0,
    )
    defaults.update(overrides)
    return CameraCalibration(**defaults)


# ---- basic construction ----


class TestIPMTransformerInit:
    def test_with_calibration(self):
        cal = _default_calibration()
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        assert ipm.context is not None
        assert ipm.context.frame_width == 1920
        assert ipm.context.frame_height == 1080

    def test_without_calibration(self):
        ipm = IPMTransformer(1920, 1080)
        assert ipm.context is not None

    def test_current_yaw_matches_calibration(self):
        cal = _default_calibration(yaw_deg=15.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        assert ipm.current_yaw == pytest.approx(15.0, abs=0.01)

    def test_current_yaw_defaults_to_zero(self):
        ipm = IPMTransformer(1920, 1080)
        assert ipm.current_yaw == pytest.approx(0.0)

    def test_stores_calibration_params(self):
        cal = _default_calibration(pitch_deg=10.0, height_m=2.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        assert ipm._calibration.pitch_deg == 10.0
        assert ipm._calibration.height_m == 2.0


# ---- update_yaw (logic-only tests, no real cv2 needed) ----


class TestUpdateYawLogic:
    def test_update_yaw_returns_true_when_changed(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        result = ipm.update_yaw(10.0)
        assert result is True
        assert ipm.current_yaw == pytest.approx(10.0, abs=0.01)

    def test_update_yaw_returns_false_within_tolerance(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        result = ipm.update_yaw(0.3)
        assert result is False
        assert ipm.current_yaw == pytest.approx(0.0, abs=0.01)

    def test_update_yaw_with_custom_tolerance(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        result = ipm.update_yaw(5.0, tolerance=10.0)
        assert result is False

    def test_update_yaw_without_calibration_returns_false(self):
        ipm = IPMTransformer(1920, 1080)
        result = ipm.update_yaw(10.0)
        assert result is False
        assert ipm.current_yaw == 0.0

    def test_update_yaw_preserves_calibration_other_params(self):
        cal = _default_calibration(pitch_deg=10.0, roll_deg=3.0, height_m=1.5)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        ipm.update_yaw(20.0)
        assert ipm._calibration.pitch_deg == 10.0
        assert ipm._calibration.roll_deg == 3.0
        assert ipm._calibration.height_m == 1.5

    def test_repeated_yaw_updates_accumulate(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        ipm.update_yaw(5.0)
        assert ipm.current_yaw == pytest.approx(5.0)
        ipm.update_yaw(10.0)
        assert ipm.current_yaw == pytest.approx(10.0)
        ipm.update_yaw(-3.0)
        assert ipm.current_yaw == pytest.approx(-3.0)

    def test_yaw_on_boundary_exactly_triggers_update(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        result = ipm.update_yaw(0.5)
        assert result is True

    def test_yaw_just_below_boundary_skips_update(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        result = ipm.update_yaw(0.4999)
        assert result is False

    def test_negative_yaw_updates(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        result = ipm.update_yaw(-15.0)
        assert result is True
        assert ipm.current_yaw == pytest.approx(-15.0)

    def test_update_yaw_from_nonzero_yaw(self):
        cal = _default_calibration(yaw_deg=10.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        assert ipm.current_yaw == pytest.approx(10.0)
        result = ipm.update_yaw(25.0)
        assert result is True
        assert ipm.current_yaw == pytest.approx(25.0)


# ---- default ROI fallback (no calibration) ----


class TestDefaultROI:
    def test_compute_phys_area_works(self):
        ipm = IPMTransformer(1920, 1080)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area = ipm.compute_phys_area(bbox)
        assert area >= 0

    def test_update_yaw_is_noop_without_calibration(self):
        ipm = IPMTransformer(1920, 1080)
        assert ipm.update_yaw(10.0) is False
        assert ipm.current_yaw == 0.0


# ---- distance correction (logic-only, no cv2 needed) ----


class TestDistanceCorrection:
    """Tests for _distance_correction and forward_distance_m in compute_phys_area."""

    def test_near_distance_gives_correction_1(self):
        ipm = IPMTransformer(1920, 1080)
        assert ipm._distance_correction(DEFAULT_NEAR_METERS) == 1.0

    def test_below_near_distance_gives_correction_1(self):
        ipm = IPMTransformer(1920, 1080)
        assert ipm._distance_correction(1.0) == 1.0
        assert ipm._distance_correction(0.0) == 1.0

    def test_far_distance_gives_max_correction(self):
        ipm = IPMTransformer(1920, 1080)
        expected = 1.0 + DISTANCE_CORRECTION_ALPHA
        assert ipm._distance_correction(DEFAULT_FAR_METERS) == pytest.approx(
            expected, abs=1e-6
        )

    def test_beyond_far_distance_clamps_to_max(self):
        ipm = IPMTransformer(1920, 1080)
        expected = 1.0 + DISTANCE_CORRECTION_ALPHA
        assert ipm._distance_correction(100.0) == pytest.approx(expected, abs=1e-6)

    def test_mid_range_gives_half_correction(self):
        ipm = IPMTransformer(1920, 1080)
        mid = (DEFAULT_NEAR_METERS + DEFAULT_FAR_METERS) / 2.0
        expected = 1.0 + DISTANCE_CORRECTION_ALPHA * 0.5
        assert ipm._distance_correction(mid) == pytest.approx(expected, abs=1e-6)

    def test_correction_increases_monotonically(self):
        ipm = IPMTransformer(1920, 1080)
        prev = 0.0
        for d in range(0, 30):
            c = ipm._distance_correction(float(d))
            assert c >= prev
            prev = c

    def test_compute_phys_area_without_distance_returns_base(self):
        """When forward_distance_m is None the area is uncorrected."""
        ipm = IPMTransformer(1920, 1080)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area_base = ipm.compute_phys_area(bbox)
        area_none = ipm.compute_phys_area(bbox, forward_distance_m=None)
        assert area_base == pytest.approx(area_none)

    def test_compute_phys_area_near_distance_unchanged(self):
        """At near range the correction is 1.0 so area is unchanged."""
        ipm = IPMTransformer(1920, 1080)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area_base = ipm.compute_phys_area(bbox)
        area_near = ipm.compute_phys_area(bbox, forward_distance_m=DEFAULT_NEAR_METERS)
        assert area_near == pytest.approx(area_base)

    def test_compute_phys_area_far_distance_increases_area(self):
        """At far range the correction > 1.0 so area is larger."""
        ipm = IPMTransformer(1920, 1080)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area_base = ipm.compute_phys_area(bbox)
        area_far = ipm.compute_phys_area(bbox, forward_distance_m=DEFAULT_FAR_METERS)
        assert area_far > area_base

    def test_compute_phys_area_correction_scales_correctly(self):
        """Verify area * correction == area with distance."""
        ipm = IPMTransformer(1920, 1080)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area_base = ipm.compute_phys_area(bbox)
        d = 20.0
        area_d = ipm.compute_phys_area(bbox, forward_distance_m=d)
        correction = ipm._distance_correction(d)
        assert area_d == pytest.approx(area_base * correction, rel=1e-6)


# ---- cv2-dependent tests (require real opencv) ----

_has_cv2 = _cv2_available


@pytest.mark.skipif(not _has_cv2, reason="requires real cv2 for perspective math")
class TestPhysAreaWithYaw:
    def test_zero_yaw_gives_baseline_area(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area = ipm.compute_phys_area(bbox)
        assert area > 0

    def test_yaw_shift_changes_area(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area_0 = ipm.compute_phys_area(bbox)
        ipm.update_yaw(10.0)
        area_10 = ipm.compute_phys_area(bbox)
        assert area_10 != pytest.approx(area_0, abs=0.01)

    def test_symmetric_yaw_gives_similar_area(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        bbox = [0.3, 0.7, 0.7, 0.95]
        ipm.update_yaw(5.0)
        area_5 = ipm.compute_phys_area(bbox)
        ipm.update_yaw(-5.0)
        area_neg5 = ipm.compute_phys_area(bbox)
        assert area_5 == pytest.approx(area_neg5, abs=0.01)

    def test_large_yaw_gives_larger_area_deviation(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        bbox = [0.3, 0.7, 0.7, 0.95]
        area_0 = ipm.compute_phys_area(bbox)
        ipm.update_yaw(5.0)
        area_5 = ipm.compute_phys_area(bbox)
        ipm.update_yaw(30.0)
        area_30 = ipm.compute_phys_area(bbox)
        assert abs(area_30 - area_0) > abs(area_5 - area_0)


@pytest.mark.skipif(not _has_cv2, reason="requires real cv2 for perspective math")
class TestPixelToOffsetWithYaw:
    def test_offset_changes_with_yaw(self):
        cal = _default_calibration(yaw_deg=0.0)
        ipm = IPMTransformer(1920, 1080, calibration=cal)
        pixel_point = (960, 800)
        dx0, dy0 = ipm.pixel_to_offset(pixel_point)
        ipm.update_yaw(15.0)
        dx15, dy15 = ipm.pixel_to_offset(pixel_point)
        assert dx15 != pytest.approx(dx0, abs=0.01)
        assert dy15 != pytest.approx(dy0, abs=0.01)

    def test_offset_returns_zero_for_none(self):
        ipm = IPMTransformer(1920, 1080)
        assert ipm.pixel_to_offset(None) == (0.0, 0.0)

    def test_offset_clamps_to_frame_bounds(self):
        ipm = IPMTransformer(1920, 1080)
        dx, dy = ipm.pixel_to_offset((1920, 1080))
        assert np.isfinite(dx)
        assert np.isfinite(dy)


@pytest.mark.skipif(not _has_cv2, reason="requires real cv2 for perspective math")
class TestWarpToBEV:
    def test_warp_to_bev_works(self):
        ipm = IPMTransformer(1920, 1080)
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        bev = ipm.warp_to_bev(frame)
        assert bev.shape[0] > 0
        assert bev.shape[1] > 0
