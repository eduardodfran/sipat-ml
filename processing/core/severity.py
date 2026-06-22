from typing import Any

from ..config.settings import (
    CONFIDENCE_MODERATE_CAP,
    CONFIDENCE_SEVERE_CAP,
    SEVERITY_MINOR_AREA_M2,
    SEVERITY_MODERATE_AREA_M2,
)

_SEVERITY_ORDER: dict[str, int] = {"Minor": 0, "Moderate": 1, "Severe": 2}


def severity_value(severity: str | None) -> int:
    return _SEVERITY_ORDER.get(severity or "Minor", 0)


def area_to_severity(area_m2: float | None) -> str:
    """Map IPM physical area (m²) to DPWH-aligned severity."""
    if area_m2 is None or area_m2 < SEVERITY_MINOR_AREA_M2:
        return "Minor"
    if area_m2 < SEVERITY_MODERATE_AREA_M2:
        return "Moderate"
    return "Severe"


def frame_area_pct_to_severity(bbox: list[float]) -> str:
    """Frame-area heuristic severity based on normalized bbox.

    A pothole filling >2% of the image is Moderate, >6% is Severe.
    These correspond roughly to DPWH/FHWA depth-based thresholds
    at typical detection distances (5-15m).
    """
    xmin, ymin, xmax, ymax = bbox
    area_pct = round(((xmax - xmin) * (ymax - ymin)) * 100, 2)
    if area_pct < 2:
        return "Minor"
    if area_pct <= 6:
        return "Moderate"
    return "Severe"


def fuse_severity(
    ipm_severity: str, frame_severity: str, avg_confidence: float
) -> str:
    """Combine IPM, frame, and confidence into final severity.

    Takes the lower of IPM and frame severity, then caps by confidence.
    """
    result = frame_severity if severity_value(ipm_severity) > severity_value(frame_severity) else ipm_severity
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
