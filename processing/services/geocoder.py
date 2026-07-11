"""Reverse geocoding service using Nominatim (OpenStreetMap).

Converts lat/lng coordinates to human-readable addresses.
Respects Nominatim usage policy: max 1 req/sec, proper User-Agent.

Usage:
    from services.geocoder import reverse_geocode
    addr = reverse_geocode(14.5547, 121.0509)
    # {'street': 'EDSA', 'barangay': 'Guadalupe', 'city': 'Makati', ...}
"""

from __future__ import annotations

import time
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "sipat-road-hazard/1.0 (thesis-project)"
_MIN_INTERVAL = 1.0  # seconds between requests (Nominatim policy)

_last_request_time: float = 0.0


def _throttle() -> None:
    """Enforce minimum interval between Nominatim requests."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _extract_address_parts(addr: dict[str, Any]) -> dict[str, str | None]:
    """Extract structured address from Nominatim response."""
    def get(key: str) -> str | None:
        v = addr.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    return {
        "street": get("road") or get("footway") or get("path"),
        "barangay": get("suburb") or get("neighbourhood") or get("quarter"),
        "city": get("city") or get("town") or get("municipality") or get("village"),
        "province": get("state"),
        "region": get("region"),
        "country": get("country"),
        "formatted_address": get("display_name"),
    }


def reverse_geocode(lat: float, lng: float) -> dict[str, str | None] | None:
    """Reverse geocode a coordinate to an address.

    Returns a dict with keys: street, barangay, city, province, region,
    country, formatted_address. Returns None on failure (network error,
    timeout, rate limit, etc.) — never raises.
    """
    try:
        _throttle()
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "addressdetails": 1,
                "accept-language": "en",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data or "address" not in data:
            logger.warning("Nominatim returned no address for %s, %s", lat, lng)
            return None

        result = _extract_address_parts(data["address"])

        # If we got nothing useful, return None
        if not any(result.values()):
            logger.warning("Nominatim returned empty address fields for %s, %s", lat, lng)
            return None

        return result

    except requests.RequestException as exc:
        logger.warning("Geocoding failed for %s, %s: %s", lat, lng, exc)
        return None
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Geocoding parse error for %s, %s: %s", lat, lng, exc)
        return None


def geocode_pothole(client: Any, pothole_id: int, lat: float, lng: float) -> bool:
    """Geocode a single pothole and update the DB row.

    Returns True if the DB was updated, False otherwise.
    """
    addr = reverse_geocode(lat, lng)
    if addr is None:
        return False

    try:
        client.schema("public").from_("verified_potholes").update({
            "street": addr.get("street"),
            "barangay": addr.get("barangay"),
            "city": addr.get("city"),
            "province": addr.get("province"),
            "region": addr.get("region"),
            "country": addr.get("country"),
            "formatted_address": addr.get("formatted_address"),
            "address_geocoded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).eq("id", pothole_id).execute()
        logger.info("Geocoded pothole %d: %s", pothole_id, addr.get("formatted_address"))
        return True
    except Exception as exc:
        logger.warning("Failed to update pothole %d with address: %s", pothole_id, exc)
        return False
