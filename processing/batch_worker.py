"""
Legacy entry point — delegates to pipeline.worker.RideProcessor.

Kept for backward compatibility with main.py and CLI usage.
New code should import from pipeline.worker directly.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from pipeline.worker import RideProcessor

load_dotenv()


# ---- re-exported for main.py compatibility ----

def _build_supabase_client():
    return RideProcessor()._supabase


def _friendly_error_message(exc: Exception) -> str:
    return RideProcessor._friendly_error(exc, "operation")


def _mark_failed(supabase, ride_id: str, error_message: str) -> None:
    supabase.table("rides_metadata").update(
        {"status": "failed", "error_log": error_message}
    ).eq("id", ride_id).execute()


def _process_ride(supabase, ride: dict) -> dict:
    """Backward-compatible wrapper. Ignores passed supabase client
    in favor of RideProcessor's own client."""
    processor = RideProcessor()
    return processor.process_ride(ride)


# ---- CLI entry points ----


def process_next_queued_ride():
    processor = RideProcessor()
    return processor.process_next_queued()


def process_ride_by_id(ride_id: str):
    processor = RideProcessor()
    return processor.process_by_id(ride_id)


if __name__ == "__main__":
    while True:
        try:
            result = process_next_queued_ride()
            if result is None:
                print("No queued rides found. Sleeping for 10 minutes...")
            else:
                print(
                    f"Finished ride {result['ride_id']} with {result['raw_detection_count']} "
                    f"raw detections. Sleeping for 10 minutes..."
                )
        except Exception as exc:
            print(f"Batch worker cycle failed: {exc}")
