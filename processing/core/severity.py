from typing import Any

from ..config.settings import (
    CONFIDENCE_MODERATE_CAP,
    CONFIDENCE_SEVERE_CAP,
    SEVERITY_MINOR_AREA_M2,
    SEVERITY_MODERATE_AREA_M2,
)

_SEVERITY_ORDER: dict[str, int] = {"Minor": 0, "Moderate": 1, "Severe": 2}

REFERENCE_DISTANCE_M = 10.0
FRAME_SEVERITY_MINOR_PCT = 2.0
FRAME_SEVERITY_SEVERE_PCT = 6.0
IPM_SEVERITY_WEIGHT = 0.70


def severity_value(severity: str | None) -> int:
    return _SEVERITY_ORDER.get(severity or "Minor", 0)


def _severity_from_value(value: float) -> str:
    """Quantize a continuous severity value (0..2) to a discrete label."""
    if value < 0.5:
        return "Minor"
    if value < 1.5:
        return "Moderate"
    return "Severe"


def area_to_severity(area_m2: float | None) -> str:
    """Map IPM physical area (m²) to DPWH-aligned severity."""
    if area_m2 is None or area_m2 < SEVERITY_MINOR_AREA_M2:
        return "Minor"
    if area_m2 < SEVERITY_MODERATE_AREA_M2:
        return "Moderate"
    return "Severe"


def frame_area_pct_to_severity(bbox: list[float], forward_distance_m: float | None = None) -> str:
    """Frame-area heuristic severity based on normalized bbox.

    If ``forward_distance_m`` is known (from IPM ``pixel_to_offset``) the
    apparent area-% is normalised by inverse-square so that the thresholds
    correspond to a reference viewing distance (10 m).  This removes the
    strong distance bias of the raw 2% / 6% heuristic.
    """
    xmin, ymin, xmax, ymax = bbox
    area_pct = ((xmax - xmin) * (ymax - ymin)) * 100.0

    if forward_distance_m and forward_distance_m > 0.1:
        area_pct = area_pct * (forward_distance_m / REFERENCE_DISTANCE_M) ** 2

    area_pct = round(area_pct, 2)
    if area_pct < FRAME_SEVERITY_MINOR_PCT:
        return "Minor"
    if area_pct <= FRAME_SEVERITY_SEVERE_PCT:
        return "Moderate"
    return "Severe"


def fuse_severity(
    ipm_severity: str,
    frame_severity: str,
    avg_confidence: float,
    ipm_weight: float = IPM_SEVERITY_WEIGHT,
) -> str:
    """Combine IPM, frame, and confidence into final severity.

    Uses a weighted blend (default 70% IPM / 30% frame) instead of the
    previous pessimistic lower-of-two rule.  IPM-based severity is more
    physically grounded because it accounts for camera perspective and
    viewing geometry, so it carries more weight.  Confidence caps are
    preserved to avoid overrating uncertain detections.
    """
    w = max(0.0, min(1.0, ipm_weight))
    blended = (
        w * severity_value(ipm_severity)
        + (1.0 - w) * severity_value(frame_severity)
    )
    result = _severity_from_value(blended)

    if avg_confidence < CONFIDENCE_MODERATE_CAP:
        result = "Minor"
    elif avg_confidence < CONFIDENCE_SEVERE_CAP and severity_value(result) > 1:
        result = "Moderate"
    return result


def escalate_severity(current: str, incoming: str) -> str:
    """Return the higher (worse) of two severities."""
    return incoming if severity_value(incoming) >= severity_value(current) else current


# ---- backward-compat alias for utils.damage_severity ----

calculate_severity = frame_area_pct_to_severity
