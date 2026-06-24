"""
Legacy entry point — delegates to pipeline.worker.RideProcessor.

Kept for backward compatibility with CLI usage.
New code should import from pipeline.worker directly.
"""

import logging
import time

from pipeline.worker import RideProcessor

logger = logging.getLogger(__name__)

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
                logger.info("No queued rides found. Sleeping for 10 minutes...")
            else:
                logger.info(
                    "Finished ride %s with %d raw detections. Sleeping for 10 minutes...",
                    result["ride_id"], result["raw_detection_count"]
                )
        except Exception as exc:
            logger.error("Batch worker cycle failed: %s", exc)
        time.sleep(600)
