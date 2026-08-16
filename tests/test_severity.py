from __future__ import annotations

import pytest

from processing.core.severity import (
    area_to_severity,
    escalate_severity,
    frame_area_pct_to_severity,
    fuse_severity,
    severity_value,
)
from processing.config.settings import (
    CONFIDENCE_MODERATE_CAP,
    CONFIDENCE_SEVERE_CAP,
    SEVERITY_MINOR_AREA_M2,
    SEVERITY_MODERATE_AREA_M2,
)


class TestSeverityValue:
    def test_minor_returns_0(self):
        assert severity_value("Minor") == 0

    def test_moderate_returns_1(self):
        assert severity_value("Moderate") == 1

    def test_severe_returns_2(self):
        assert severity_value("Severe") == 2

    def test_none_defaults_to_0(self):
        assert severity_value(None) == 0

    def test_unknown_string_returns_0(self):
        assert severity_value("Unknown") == 0


class TestAreaToSeverity:
    def test_none_returns_minor(self):
        assert area_to_severity(None) == "Minor"

    def test_zero_returns_minor(self):
        assert area_to_severity(0.0) == "Minor"

    def test_below_minor_threshold_returns_minor(self):
        assert area_to_severity(SEVERITY_MINOR_AREA_M2 - 0.01) == "Minor"

    def test_at_minor_threshold_returns_moderate(self):
        assert area_to_severity(SEVERITY_MINOR_AREA_M2) == "Moderate"

    def test_between_thresholds_returns_moderate(self):
        mid = (SEVERITY_MINOR_AREA_M2 + SEVERITY_MODERATE_AREA_M2) / 2
        assert area_to_severity(mid) == "Moderate"

    def test_at_moderate_threshold_returns_severe(self):
        assert area_to_severity(SEVERITY_MODERATE_AREA_M2) == "Severe"

    def test_above_moderate_threshold_returns_severe(self):
        assert area_to_severity(1.0) == "Severe"

    def test_negative_returns_minor(self):
        assert area_to_severity(-0.5) == "Minor"


class TestFrameAreaPctToSeverity:
    def test_small_bbox_returns_minor(self):
        assert frame_area_pct_to_severity([0.0, 0.0, 0.1, 0.1]) == "Minor"

    def test_at_2_pct_returns_moderate(self):
        assert frame_area_pct_to_severity([0.0, 0.0, 0.2, 0.1]) == "Moderate"

    def test_at_6_pct_returns_moderate(self):
        assert frame_area_pct_to_severity([0.0, 0.0, 0.3, 0.2]) == "Moderate"

    def test_above_6_pct_returns_severe(self):
        assert frame_area_pct_to_severity([0.0, 0.0, 0.5, 0.5]) == "Severe"

    def test_full_frame_returns_severe(self):
        assert frame_area_pct_to_severity([0.0, 0.0, 1.0, 1.0]) == "Severe"

    def test_zero_area_returns_minor(self):
        assert frame_area_pct_to_severity([0.5, 0.5, 0.5, 0.5]) == "Minor"


class TestFuseSeverity:
    def test_weighted_blend_both_minor_keeps_minor(self):
        assert fuse_severity("Minor", "Minor", 0.8) == "Minor"

    def test_weighted_blend_ipm_severe_frame_minor_gives_moderate(self):
        # 0.70 * 2 (Severe) + 0.30 * 0 (Minor) = 1.4 → round to Moderate (<1.5 threshold)
        result = fuse_severity("Severe", "Minor", 0.8)
        assert result == "Moderate"

    def test_weighted_blend_ipm_minor_frame_severe_gives_moderate(self):
        # 0.70 * 0 (Minor) + 0.30 * 2 (Severe) = 0.6 → Minor-Moderate boundary (≥0.5 → Moderate)
        result = fuse_severity("Minor", "Severe", 0.8)
        assert result == "Moderate"

    def test_equal_severities_keeps_value(self):
        assert fuse_severity("Moderate", "Moderate", 0.8) == "Moderate"

    def test_both_severe_high_conf_gives_severe(self):
        assert fuse_severity("Severe", "Severe", 0.8) == "Severe"

    def test_low_confidence_caps_to_minor(self):
        assert fuse_severity("Severe", "Severe", CONFIDENCE_MODERATE_CAP - 0.01) == "Minor"

    def test_moderate_confidence_caps_severe_to_moderate(self):
        # Both Severe → blended = 2.0, but conf < SEVERE_CAP → Moderate
        assert fuse_severity("Severe", "Severe", CONFIDENCE_MODERATE_CAP) == "Moderate"

    def test_high_confidence_keeps_severe(self):
        assert fuse_severity("Severe", "Severe", CONFIDENCE_SEVERE_CAP) == "Severe"

    def test_confidence_between_caps_keeps_moderate(self):
        conf = (CONFIDENCE_MODERATE_CAP + CONFIDENCE_SEVERE_CAP) / 2
        assert fuse_severity("Moderate", "Moderate", conf) == "Moderate"

    def test_confidence_below_moderate_cap_forces_minor_even_if_both_severe(self):
        assert fuse_severity("Severe", "Severe", CONFIDENCE_MODERATE_CAP - 0.05) == "Minor"

    def test_moderate_severity_with_high_confidence_keeps_moderate(self):
        assert fuse_severity("Moderate", "Moderate", CONFIDENCE_SEVERE_CAP) == "Moderate"

    def test_custom_weight_full_ipm(self):
        # ipm_weight=1.0 → only IPM counts
        assert fuse_severity("Severe", "Minor", 0.9, ipm_weight=1.0) == "Severe"

    def test_custom_weight_zero_ipm_uses_frame(self):
        # ipm_weight=0.0 → only frame counts
        assert fuse_severity("Minor", "Severe", 0.9, ipm_weight=0.0) == "Severe"

    def test_weight_clamped_to_0_1(self):
        # ipm_weight=2.0 is clamped to 1.0 → IPM-only
        assert fuse_severity("Severe", "Minor", 0.9, ipm_weight=2.0) == "Severe"
        # ipm_weight=-0.5 is clamped to 0.0 → frame-only
        assert fuse_severity("Minor", "Severe", 0.9, ipm_weight=-0.5) == "Severe"


class TestDistanceAwareFrameSeverity:
    def test_no_distance_uses_baseline_thresholds(self):
        # 0.2 × 0.1 = 2% exactly → Moderate at baseline
        assert frame_area_pct_to_severity([0.0, 0.0, 0.2, 0.1]) == "Moderate"

    def test_close_distance_makes_severity_lower(self):
        # At 5m (half of 10m reference), area % is halved: 2% → 0.5% → Minor
        bbox = [0.0, 0.0, 0.2, 0.1]  # 2%
        assert frame_area_pct_to_severity(bbox, forward_distance_m=5.0) == "Minor"

    def test_far_distance_makes_severity_higher(self):
        # At 20m (2x reference), area % quadruples: 2% → 8% → Severe
        bbox = [0.0, 0.0, 0.2, 0.1]  # 2%
        assert frame_area_pct_to_severity(bbox, forward_distance_m=20.0) == "Severe"

    def test_at_reference_distance_same_as_baseline(self):
        bbox = [0.0, 0.0, 0.2, 0.1]  # 2%
        baseline = frame_area_pct_to_severity(bbox, forward_distance_m=None)
        at_ref = frame_area_pct_to_severity(bbox, forward_distance_m=10.0)
        assert baseline == at_ref

    def test_zero_or_negative_distance_ignored(self):
        bbox = [0.0, 0.0, 0.1, 0.1]  # 1% → Minor
        assert frame_area_pct_to_severity(bbox, forward_distance_m=0.0) == "Minor"
        assert frame_area_pct_to_severity(bbox, forward_distance_m=-5.0) == "Minor"


class TestEscalateSeverity:
    def test_same_severity_returns_same(self):
        assert escalate_severity("Minor", "Minor") == "Minor"
        assert escalate_severity("Moderate", "Moderate") == "Moderate"
        assert escalate_severity("Severe", "Severe") == "Severe"

    def test_escalates_minor_to_moderate(self):
        assert escalate_severity("Minor", "Moderate") == "Moderate"

    def test_escalates_moderate_to_severe(self):
        assert escalate_severity("Moderate", "Severe") == "Severe"

    def test_escalates_minor_to_severe(self):
        assert escalate_severity("Minor", "Severe") == "Severe"

    def test_keeps_higher_on_left(self):
        assert escalate_severity("Severe", "Minor") == "Severe"

    def test_keeps_higher_on_right(self):
        assert escalate_severity("Moderate", "Severe") == "Severe"

    def test_repeated_escalation(self):
        result = "Minor"
        result = escalate_severity(result, "Moderate")
        result = escalate_severity(result, "Severe")
        assert result == "Severe"
