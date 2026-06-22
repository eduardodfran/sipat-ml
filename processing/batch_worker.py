"""
Legacy entry point — delegates to pipeline.worker.RideProcessor.

Kept for backward compatibility with CLI usage.
New code should import from pipeline.worker directly.
"""

from pipeline.worker import RideProcessor


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
