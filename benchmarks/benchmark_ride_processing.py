"""Benchmark full ride processing time (end-to-end)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from ultralytics import YOLO

# Add processing/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from config.settings import MODEL_PATH, YOLO_CONFIDENCE
from utils.gps_processor import GPSProcessor

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def create_test_video(output_path: Path, num_frames: int = 150, fps: float = 30.0) -> Path:
    """Create a synthetic test video for benchmarking."""
    width, height = 1280, 720
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))

    for i in range(num_frames):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        # Road surface
        frame[height // 2:, :] = [80, 80, 80]
        # Lane markings
        cv2.line(frame, (width // 2, height // 2), (width // 2, height), (255, 255, 255), 3)
        # Some variation
        color_val = 60 + (i % 40)
        frame[height // 2:, :] = [color_val, color_val, color_val]
        writer.write(frame)

    writer.release()
    return output_path


def create_test_gps(num_frames: int = 150, fps: float = 30.0) -> dict:
    """Create synthetic GPS data matching the video."""
    base_lat, base_lng = 14.5995, 120.9842  # Manila
    samples = []
    for i in range(num_frames):
        t = i / fps
        samples.append({
            "timestamp_seconds": t,
            "lat": base_lat + (i * 0.00001),
            "lng": base_lng + (i * 0.00001),
            "heading": 45.0 + (i * 0.1),
            "speed": 30.0,
        })
    return samples


def benchmark_ride_processing(model: YOLO, video_path: Path, gps_data: list) -> dict:
    """Benchmark the full ride processing pipeline."""
    from processing.detection_batch_builder import DetectionBatchBuilder

    # Create mock services
    mock_supabase = MagicMock()
    mock_supabase.storage.from_().upload.return_value = None

    # Create GPS processor
    gps_processor = GPSProcessor(gps_data)

    # Create builder
    builder = DetectionBatchBuilder(
        ride_id="benchmark-test",
        user_id="benchmark-user",
        supabase=mock_supabase,
        model=model,
        supabase_url="http://mock",
        progress_callback=None,
    )

    # Time the full processing
    start_time = time.perf_counter()
    detections = builder.build(video_path, gps_processor)
    elapsed = time.perf_counter() - start_time

    return {
        "total_time_sec": round(elapsed, 2),
        "video_frames": num_frames,
        "detections_found": len(detections),
        "frames_per_second": round(num_frames / elapsed, 2) if elapsed > 0 else 0,
    }


def main():
    print("=" * 60)
    print("Full Ride Processing Benchmark")
    print("=" * 60)

    # Check model exists
    if not MODEL_PATH.exists():
        print(f"ERROR: Model not found at {MODEL_PATH}")
        print("Skipping ride processing benchmark.")
        return None

    # Load model
    print(f"Loading model from {MODEL_PATH}...")
    model = YOLO(str(MODEL_PATH))
    print("Model loaded successfully.")

    # Create test video
    global num_frames
    num_frames = 150
    fps = 30.0

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "test_ride.mp4"
        print(f"Creating test video ({num_frames} frames, {fps} fps)...")
        create_test_video(video_path, num_frames, fps)

        # Create GPS data
        gps_data = create_test_gps(num_frames, fps)

        # Run benchmark
        print("\nRunning benchmark...")
        results = benchmark_ride_processing(model, video_path, gps_data)

    # Save results
    output = {
        "benchmark": "ride_processing",
        "model_path": str(MODEL_PATH),
        "config": {
            "num_frames": num_frames,
            "fps": fps,
        },
        "results": results,
    }

    output_path = RESULTS_DIR / "ride_processing.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to {output_path}")
    print(f"\nSummary:")
    print(f"  Total time: {results['total_time_sec']:.1f}s")
    print(f"  Detections found: {results['detections_found']}")
    print(f"  Processing speed: {results['frames_per_second']:.1f} frames/sec")

    return output


if __name__ == "__main__":
    main()
