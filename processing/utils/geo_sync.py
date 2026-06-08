from __future__ import annotations

from bisect import bisect_left
from typing import Any


def _normalize_gps_samples(
    gps_data: list[dict[str, Any]],
) -> list[tuple[float, float, float]]:
    samples: list[tuple[float, float, float]] = []
    for item in gps_data:
        try:
            timestamp_seconds = float(item["timestamp_seconds"])
            lat = float(item["lat"])
            lng = float(item["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((timestamp_seconds, lat, lng))

    if not samples:
        raise ValueError("GPS data must include timestamp_seconds, lat, and lng values")

    samples.sort(key=lambda row: row[0])
    return samples


def interpolate_coordinate_at_time(
    gps_data: list[dict[str, Any]],
    current_time: float,
) -> tuple[float, float]:
    samples = _normalize_gps_samples(gps_data)
    timestamps = [sample[0] for sample in samples]

    position = bisect_left(timestamps, current_time)
    if position <= 0:
        return samples[0][1], samples[0][2]
    if position >= len(samples):
        return samples[-1][1], samples[-1][2]

    previous_time = timestamps[position - 1]
    next_time = timestamps[position]
    previous_lat, previous_lng = samples[position - 1][1], samples[position - 1][2]
    next_lat, next_lng = samples[position][1], samples[position][2]

    if current_time == previous_time:
        return previous_lat, previous_lng
    if current_time == next_time:
        return next_lat, next_lng
    if next_time == previous_time:
        return previous_lat, previous_lng

    fraction = (current_time - previous_time) / (next_time - previous_time)
    lat = previous_lat + (next_lat - previous_lat) * fraction
    lng = previous_lng + (next_lng - previous_lng) * fraction
    return lat, lng