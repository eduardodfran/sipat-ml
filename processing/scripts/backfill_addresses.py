"""Backfill address data for all existing potholes using corrected geocoder.

Re-geocodes every row in verified_potholes that has coordinates, updating
the address columns (street, barangay, city, province, region, country,
formatted_address, address_geocoded_at).

Usage:
    cd sipat-ml/processing
    python -m scripts.backfill_addresses [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Ensure parent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

from services.supabase_client import SupabaseService  # noqa: E402
from services.geocoder import reverse_geocode         # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def fetch_potholes(client, limit: int | None) -> list[dict]:
    query = (
        client.table("verified_potholes")
        .select("id, consolidated_latitude, consolidated_longitude")
        .not_.is_("consolidated_latitude", "null")
        .not_.is_("consolidated_longitude", "null")
    )
    if limit:
        query = query.limit(limit)
    return (query.execute().data) or []


def update_pothole(client, pothole_id: int, addr: dict) -> None:
    client.table("verified_potholes").update({
        "street": addr.get("street"),
        "barangay": addr.get("barangay"),
        "city": addr.get("city"),
        "province": addr.get("province"),
        "region": addr.get("region"),
        "country": addr.get("country"),
        "formatted_address": addr.get("formatted_address"),
        "address_geocoded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }).eq("id", pothole_id).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill address data for potholes")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max potholes to process")
    args = parser.parse_args()

    svc = SupabaseService()
    client = svc.client

    logger.info("Fetching potholes with coordinates...")
    potholes = fetch_potholes(client, args.limit)
    total = len(potholes)
    logger.info("Found %d potholes to backfill", total)

    if total == 0:
        logger.info("Nothing to do.")
        return

    updated = 0
    failed = 0
    skipped = 0

    for i, p in enumerate(potholes, 1):
        pid = p["id"]
        lat = p["consolidated_latitude"]
        lng = p["consolidated_longitude"]

        addr = reverse_geocode(lat, lng)
        if addr is None:
            skipped += 1
            logger.warning("[%d/%d] id=%d — geocode failed, skipping", i, total, pid)
            continue

        summary = f"street={addr.get('street')}, brgy={addr.get('barangay')}, city={addr.get('city')}"

        if args.dry_run:
            logger.info("[%d/%d] id=%d — %s", i, total, pid, summary)
            updated += 1
        else:
            try:
                update_pothole(client, pid, addr)
                updated += 1
                logger.info("[%d/%d] id=%d — %s", i, total, pid, summary)
            except Exception as exc:
                failed += 1
                logger.error("[%d/%d] id=%d — DB update failed: %s", i, total, pid, exc)

    logger.info(
        "Done. updated=%d, failed=%d, skipped=%d, total=%d",
        updated, failed, skipped, total,
    )


if __name__ == "__main__":
    main()
