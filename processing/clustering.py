import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN


def cluster_pothole_detections(raw_data_list, max_distance_meters=3.0, min_detections=3):
    """
    Clusters frame-by-frame GPS detections into unique, real-world pothole entities.
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

        cleaned_potholes.append({
            "lat": round(centroid_lat, 6),
            "lng": round(centroid_lng, 6),
            "detection_count": total_hits,
            "image_url": image_url,
            "max_area_m2": float(cluster_subset["phys_area_m2"].max()),
            "user_detections": _aggregate_user_detections(cluster_subset),
        })

    return cleaned_potholes


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
