from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

from ..config.settings import EARTH_RADIUS_METERS, MERGE_RADIUS_METERS, CLUSTER_MIN_DETECTIONS
from .severity import severity_value


class PotholeClusterer:
    """Clusters frame-by-frame GPS detections into unique real-world potholes."""

    def __init__(
        self,
        max_distance_meters: float = MERGE_RADIUS_METERS,
        min_detections: int = CLUSTER_MIN_DETECTIONS,
    ) -> None:
        self.max_distance_meters = max_distance_meters
        self.min_detections = min_detections

    def cluster(self, raw_data_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not raw_data_list:
            return []

        df = pd.DataFrame(raw_data_list)
        coordinates_matrix = np.radians(df[["lat", "lng"]].values)

        epsilon_radians = self.max_distance_meters / EARTH_RADIUS_METERS
        db = DBSCAN(eps=epsilon_radians, min_samples=self.min_detections, metric="haversine")
        db.fit(coordinates_matrix)
        df["cluster_id"] = db.labels_

        cleaned_potholes: list[dict[str, Any]] = []
        for cluster_id in df["cluster_id"].unique():
            if cluster_id == -1:
                continue

            subset = df[df["cluster_id"] == cluster_id]
            pothole = self._build_pothole(subset)
            cleaned_potholes.append(pothole)

        return cleaned_potholes

    @staticmethod
    def _build_pothole(subset: pd.DataFrame) -> dict[str, Any]:
        return {
            "lat": round(float(subset["lat"].mean()), 6),
            "lng": round(float(subset["lng"].mean()), 6),
            "detection_count": len(subset),
            "image_url": _first_image_url(subset),
            "max_area_m2": _max_phys_area(subset),
            "max_frame_severity": _max_frame_severity(subset),
            "avg_confidence": _avg_confidence(subset),
            "user_detections": _aggregate_user_detections(subset),
        }


# ---- static helpers (also exported for direct functional use) ----


def _first_image_url(subset: pd.DataFrame) -> str | None:
    if "image_url" not in subset.columns:
        return None
    non_null = subset["image_url"].dropna()
    return str(non_null.iloc[0]) if len(non_null) > 0 else None


def _max_phys_area(subset: pd.DataFrame) -> float:
    """Per-ride MAX phys_area_m2, then MAX across rides."""
    if "phys_area_m2" not in subset.columns:
        return 0.0
    if "ride_id" not in subset.columns:
        return float(subset["phys_area_m2"].max())
    per_ride_max = subset.groupby("ride_id")["phys_area_m2"].max()
    return float(per_ride_max.max())


def _max_frame_severity(subset: pd.DataFrame) -> str:
    if "severity" not in subset.columns:
        return "Minor"
    max_sev = "Minor"
    for _, row in subset.iterrows():
        s = str(row.get("severity", "Minor"))
        if severity_value(s) > severity_value(max_sev):
            max_sev = s
    return max_sev


def _avg_confidence(subset: pd.DataFrame) -> float:
    if "confidence" not in subset.columns or subset["confidence"].empty:
        return 0.0
    return float(subset["confidence"].mean())


def _aggregate_user_detections(subset: pd.DataFrame) -> list[dict[str, Any]]:
    if "user_id" not in subset.columns:
        return []
    best: dict[str, float] = {}
    for _, row in subset.iterrows():
        uid = row.get("user_id")
        ts = row.get("video_timestamp")
        if uid is None or ts is None:
            continue
        if uid not in best or ts < best[uid]:
            best[uid] = ts
    return sorted(
        [{"user_id": uid, "video_timestamp": ts} for uid, ts in best.items()],
        key=lambda x: x["video_timestamp"],
    )
