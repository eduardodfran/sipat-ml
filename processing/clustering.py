import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

_SEVERITY_ORDER = {"Minor": 0, "Moderate": 1, "Severe": 2}


def cluster_pothole_detections(raw_data_list, max_distance_meters=15.0, min_detections=3):
    """
    Clusters frame-by-frame GPS detections into unique, real-world pothole entities.

    The 15m radius accounts for consumer phone GPS drift (±5-8m typical).
    Must match MERGE_RADIUS_METERS in batch_worker.py and the default
    radius_meters in the get_pothole_detectors SQL RPC.
    """
    if not raw_data_list:
        return []

    df = pd.DataFrame(raw_data_list)
    coordinates_matrix = np.radians(df[["lat", "lng"]].values)

    EARTH_RADIUS_METERS = 6371008.8
    epsilon_radians = max_distance_meters / EARTH_RADIUS_METERS

    db = DBSCAN(
        eps=epsilon_radians,
        min_samples=min_detections,
        metric="haversine"
    )
    db.fit(coordinates_matrix)
    df["cluster_id"] = db.labels_

    cleaned_potholes = []
    for cluster_id in df["cluster_id"].unique():
        if cluster_id == -1:
            continue

        cluster_subset = df[df["cluster_id"] == cluster_id]
        centroid_lat = cluster_subset["lat"].mean()
        centroid_lng = cluster_subset["lng"].mean()
        total_hits = len(cluster_subset)
        image_url = None
        if "image_url" in cluster_subset.columns:
            non_null = cluster_subset["image_url"].dropna()
            if len(non_null) > 0:
                image_url = non_null.iloc[0]

        max_area = _max_phys_area_across_rides(cluster_subset)
        frame_sev = _max_frame_severity(cluster_subset)
        avg_conf = _avg_confidence(cluster_subset)
        print(f"  cluster: {total_hits} dets, area={max_area:.4f}m², "
              f"frame_severity={frame_sev}, avg_confidence={avg_conf:.3f}")

        cleaned_potholes.append({
            "lat": round(centroid_lat, 6),
            "lng": round(centroid_lng, 6),
            "detection_count": total_hits,
            "image_url": image_url,
            "max_area_m2": max_area,
            "max_frame_severity": frame_sev,
            "avg_confidence": avg_conf,
            "user_detections": _aggregate_user_detections(cluster_subset),
        })

    return cleaned_potholes


def _max_phys_area_across_rides(cluster_subset: "pd.DataFrame") -> float:
    """Per-ride MAX phys_area_m2, then MAX across rides (dedup per ride)."""
    if "phys_area_m2" not in cluster_subset.columns:
        return 0.0
    if "ride_id" not in cluster_subset.columns:
        return float(cluster_subset["phys_area_m2"].max())
    per_ride_max = cluster_subset.groupby("ride_id")["phys_area_m2"].max()
    return float(per_ride_max.max())


def _max_frame_severity(cluster_subset: "pd.DataFrame") -> str:
    """Highest frame-area severity across all detections in the cluster."""
    if "severity" not in cluster_subset.columns:
        return "Minor"
    max_sev = "Minor"
    for _, row in cluster_subset.iterrows():
        s = str(row.get("severity", "Minor"))
        if _SEVERITY_ORDER.get(s, 0) > _SEVERITY_ORDER.get(max_sev, 0):
            max_sev = s
    return max_sev


def _aggregate_user_detections(cluster_subset: "pd.DataFrame") -> list[dict]:
    """Aggregate unique user_ids with earliest video_timestamp per user."""
    if "user_id" not in cluster_subset.columns:
        return []

    best: dict[str, float] = {}
    for _, row in cluster_subset.iterrows():
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


def _avg_confidence(cluster_subset: "pd.DataFrame") -> float:
    """Average YOLO confidence across all detections in the cluster."""
    if "confidence" not in cluster_subset.columns:
        return 0.0
    return float(cluster_subset["confidence"].mean())
