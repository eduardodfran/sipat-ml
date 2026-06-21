"""
Legacy module — delegates to core.clusterer.

Kept for backward compatibility. New code should import from core.clusterer.
"""

from core.clusterer import (
    PotholeClusterer,
    _max_phys_area as _max_phys_area_across_rides,
    _max_frame_severity,
    _avg_confidence,
    _aggregate_user_detections,
)


def cluster_pothole_detections(raw_data_list, max_distance_meters=15.0, min_detections=3):
    clusterer = PotholeClusterer(max_distance_meters, min_detections)
    return clusterer.cluster(raw_data_list)
