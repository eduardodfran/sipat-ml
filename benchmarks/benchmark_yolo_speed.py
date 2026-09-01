"""Benchmark YOLO inference speed per frame (CPU)."""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

# Add processing/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from config.settings import MODEL_PATH, YOLO_CONFIDENCE

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

NUM_FRAMES = 50
WARMUP_FRAMES = 5


def create_synthetic_frame(width: int = 1280, height: int = 720) -> np.ndarray:
    """Create a synthetic road-like frame for benchmarking."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    # Road surface (gray)
    frame[height // 2:, :] = [80, 80, 80]
    # Lane markings (white)
    cv2.line(frame, (width // 2, height // 2), (width // 2, height), (255, 255, 255), 3)
    # Add some noise for realism
    noise = np.random.randint(0, 20, frame.shape, dtype=np.uint8)
    frame = cv2.add(frame, noise)
    return frame


def extract_frames_from_video(video_path: Path, max_frames: int) -> list[np.ndarray]:
    """Extract frames from a video file."""
    frames = []
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Warning: Could not open {video_path}, using synthetic frames")
        return []

    while len(frames) < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        frames.append(frame)
    cap.release()
    return frames


def benchmark_yolo_speed(model: YOLO, frames: list[np.ndarray]) -> dict:
    """Benchmark YOLO inference speed on a list of frames."""
    times = []

    # Warmup
    print(f"  Warming up with {WARMUP_FRAMES} frames...")
    for i in range(min(WARMUP_FRAMES, len(frames))):
        model(frames[i], conf=YOLO_CONFIDENCE, verbose=False)

    # Benchmark
    print(f"  Benchmarking {len(frames)} frames...")
    for i, frame in enumerate(frames):
        start = time.perf_counter()
        model(frame, conf=YOLO_CONFIDENCE, verbose=False)
        elapsed = (time.perf_counter() - start) * 1000  # ms
        times.append(elapsed)

        if (i + 1) % 10 == 0:
            print(f"    Frame {i + 1}/{len(frames)}: {elapsed:.1f}ms")

    return {
        "num_frames": len(times),
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "stdev_ms": round(statistics.stdev(times), 2) if len(times) > 1 else 0,
        "p95_ms": round(sorted(times)[int(len(times) * 0.95)], 2),
        "all_times_ms": [round(t, 2) for t in times],
    }


def main():
    print("=" * 60)
    print("YOLO Inference Speed Benchmark")
    print("=" * 60)

    # Check model exists
    if not MODEL_PATH.exists():
        print(f"ERROR: Model not found at {MODEL_PATH}")
        print("Skipping YOLO speed benchmark.")
        return None

    # Load model
    print(f"Loading model from {MODEL_PATH}...")
    model = YOLO(str(MODEL_PATH))
    print("Model loaded successfully.")

    # Try to find a real video, otherwise use synthetic frames
    video_dir = Path(__file__).resolve().parent.parent.parent / "test_videos"
    frames = []

    if video_dir.exists():
        for video_file in video_dir.glob("*.mp4"):
            print(f"Using video: {video_file}")
            frames = extract_frames_from_video(video_file, NUM_FRAMES)
            if frames:
                break

    if not frames:
        print("No test video found, generating synthetic frames...")
        frames = [create_synthetic_frame() for _ in range(NUM_FRAMES)]

    # Run benchmark
    print("\nRunning benchmark...")
    results = benchmark_yolo_speed(model, frames)

    # Save results
    output = {
        "benchmark": "yolo_inference_speed",
        "model_path": str(MODEL_PATH),
        "model_size_mb": round(MODEL_PATH.stat().st_size / (1024 * 1024), 2),
        "confidence": YOLO_CONFIDENCE,
        "results": results,
    }

    output_path = RESULTS_DIR / "yolo_speed.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to {output_path}")
    print(f"\nSummary:")
    print(f"  Mean: {results['mean_ms']:.1f}ms per frame")
    print(f"  Median: {results['median_ms']:.1f}ms per frame")
    print(f"  P95: {results['p95_ms']:.1f}ms per frame")
    print(f"  Min: {results['min_ms']:.1f}ms, Max: {results['max_ms']:.1f}ms")

    return output


if __name__ == "__main__":
    main()
