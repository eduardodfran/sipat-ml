"""
SIPAT Model Evaluation Script

Compares AI-generated pothole measurements with manual ground-truth data.
Computes MAE (Mean Absolute Error) and classification accuracy.

Usage:
    python evaluate.py --video test_video.mp4 --ground-truth ground_truth.csv [--gps gps_segment_1.csv]

Ground truth CSV format:
    lat,lng,length_cm,width_cm,severity
    14.5995,120.9842,30,25,Moderate
    14.5996,120.9843,15,12,Minor
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processing.config.settings import (
    MODEL_PATH,
    YOLO_CONFIDENCE,
    FRAME_SKIP,
    CROP_TOP_RATIO,
    BLUR_THRESHOLD,
    DARK_THRESHOLD,
    EXCLUDED_CLASSES,
)
from processing.utils.camera_calibration import load_calibration
from processing.utils.ipm_transformer import IPMTransformer
from processing.utils.gps_processor import GPSProcessor
from processing.utils.geo_math import haversine_distance_meters


def load_ground_truth(csv_path: str) -> list[dict]:
    """Load ground truth measurements from CSV."""
    gt = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            length_cm = float(row["length_cm"])
            width_cm = float(row["width_cm"])
            area_m2 = (length_cm / 100.0) * (width_cm / 100.0)
            gt.append({
                "lat": float(row["lat"]),
                "lng": float(row["lng"]),
                "length_cm": length_cm,
                "width_cm": width_cm,
                "area_m2": area_m2,
                "severity": row.get("severity", "Unknown"),
            })
    return gt


def run_detection(video_path: str, gps_processor: GPSProcessor | None = None,
                  zoom_factor: float = 1.0) -> list[dict]:
    """Run YOLO + IPM detection on a video, return all detections with physical area and GPS."""
    from ultralytics import YOLO
    from processing.utils.camera_calibration import (
        CameraCalibration,
        DEFAULT_CALIBRATION_HEIGHT,
        DEFAULT_CALIBRATION_WIDTH,
    )

    model = YOLO(str(MODEL_PATH))
    calibration = load_calibration()

    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

    ipm = None
    frame_count = 0
    detections = []

    print(f"Video: {total_frames} frames, {fps:.1f} FPS, processing every {FRAME_SKIP}th frame, zoom={zoom_factor:.2f}x")

    while capture.isOpened():
        success, frame = capture.read()
        if not success:
            break

        frame_count += 1
        if frame_count % FRAME_SKIP != 0:
            continue

        h, w = frame.shape[:2]
        crop_y = 0
        if h > w and CROP_TOP_RATIO > 0:
            crop_y = int(h * CROP_TOP_RATIO)
            frame = frame[crop_y:]

        if ipm is None:
            unscaled = calibration
            if h > w:
                unscaled = CameraCalibration(
                    fx=calibration.fx, fy=calibration.fy,
                    cx=calibration.cy, cy=calibration.cx - crop_y,
                    height_m=calibration.height_m,
                    pitch_deg=calibration.pitch_deg,
                    roll_deg=calibration.roll_deg,
                    yaw_deg=calibration.yaw_deg,
                )
            actual_w = int(w)
            actual_h = int((h - crop_y) if crop_y else h)
            base_w = DEFAULT_CALIBRATION_WIDTH if h <= w else DEFAULT_CALIBRATION_HEIGHT
            base_h = (DEFAULT_CALIBRATION_HEIGHT - int(DEFAULT_CALIBRATION_HEIGHT * CROP_TOP_RATIO)) if (h > w and CROP_TOP_RATIO > 0) else DEFAULT_CALIBRATION_HEIGHT
            cal = unscaled.scaled_to_resolution(
                width_px=actual_w,
                height_px=actual_h,
                base_width_px=base_w,
                base_height_px=base_h,
                zoom_factor=zoom_factor,
            )
            print(f"Evaluation calibration: video={actual_w}x{actual_h} → fx={cal.fx:.1f} fy={cal.fy:.1f}")
            ipm = IPMTransformer(frame.shape[1], frame.shape[0], calibration=cal)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        mean_brightness = cv2.mean(gray)[0]

        if laplacian_var < BLUR_THRESHOLD or mean_brightness < DARK_THRESHOLD:
            continue

        results = model(frame, conf=YOLO_CONFIDENCE, verbose=False)

        timestamp = frame_count / fps

        for result in results:
            if not getattr(result, "boxes", None):
                continue

            for box in result.boxes:
                try:
                    bbox = box.xyxyn[0].tolist()
                except Exception:
                    continue

                class_id = int(box.cls.item())
                class_name = result.names.get(class_id, "unknown")
                if class_name in EXCLUDED_CLASSES:
                    continue

                confidence = float(box.conf.item())
                phys_area = ipm.compute_phys_area(bbox)

                det = {
                    "frame": frame_count,
                    "timestamp": timestamp,
                    "class": class_name,
                    "confidence": confidence,
                    "area_m2": phys_area,
                }

                # Interpolate GPS if available
                if gps_processor is not None:
                    try:
                        _, lat, lng, heading = gps_processor.interpolate_sample(timestamp)
                        det["lat"] = lat
                        det["lng"] = lng
                    except Exception:
                        pass

                detections.append(det)

    capture.release()
    print(f"Processed {frame_count // FRAME_SKIP} frames, found {len(detections)} detections")
    return detections


def match_detections_to_ground_truth(
    detections: list[dict],
    ground_truth: list[dict],
    match_radius_m: float = 5.0,
) -> list[dict]:
    """Match AI detections to ground truth.

    Strategy:
    1. If both detection and GT have GPS, match by proximity first (within match_radius_m),
       then by closest area among nearby matches.
    2. If GPS unavailable, match by closest area only.
    """
    matched = []
    used_gt = set()

    has_gps = any("lat" in d for d in detections)

    for det in detections:
        best_match = None
        best_score = float("inf")

        for i, gt in enumerate(ground_truth):
            if i in used_gt:
                continue

            # GPS proximity check
            if has_gps and "lat" in det:
                dist = haversine_distance_meters(
                    det["lat"], det["lng"], gt["lat"], gt["lng"]
                )
                if dist > match_radius_m:
                    continue
                # Score = distance + area difference (weighted)
                area_diff = abs(det["area_m2"] - gt["area_m2"])
                score = dist + area_diff * 10
            else:
                # Fallback: area-only matching
                score = abs(det["area_m2"] - gt["area_m2"])

            if score < best_score:
                best_score = score
                best_match = i

        if best_match is not None:
            used_gt.add(best_match)
            gt = ground_truth[best_match]
            matched.append({
                "ai_area_m2": det["area_m2"],
                "gt_area_m2": gt["area_m2"],
                "ai_class": det["class"],
                "gt_severity": gt["severity"],
                "confidence": det["confidence"],
                "abs_error": abs(det["area_m2"] - gt["area_m2"]),
                "pct_error": abs(det["area_m2"] - gt["area_m2"]) / gt["area_m2"] * 100 if gt["area_m2"] > 0 else 0,
            })

    return matched


def compute_metrics(matched: list[dict], gt_total: int) -> dict:
    """Compute MAE, RMSE, and accuracy metrics."""
    if not matched:
        return {"mae": 0, "rmse": 0, "accuracy_pct": 0, "matched": 0, "total_gt": gt_total}

    errors = [m["abs_error"] for m in matched]
    pct_errors = [m["pct_error"] for m in matched]

    mae = np.mean(errors)
    rmse = np.sqrt(np.mean(np.array(errors) ** 2))

    within_20pct = sum(1 for p in pct_errors if p <= 20)
    within_30pct = sum(1 for p in pct_errors if p <= 30)
    accuracy_20 = within_20pct / len(matched) * 100
    accuracy_30 = within_30pct / len(matched) * 100

    severity_correct = sum(
        1 for m in matched
        if _severity_from_area(m["ai_area_m2"]) == m["gt_severity"]
    )
    severity_accuracy = severity_correct / len(matched) * 100

    return {
        "mae": mae,
        "rmse": rmse,
        "accuracy_20_pct": accuracy_20,
        "accuracy_30_pct": accuracy_30,
        "severity_accuracy_pct": severity_accuracy,
        "matched": len(matched),
        "total_gt": gt_total,
        "unmatched_gt": gt_total - len(matched),
    }


def _severity_from_area(area_m2: float) -> str:
    if area_m2 < 0.03:
        return "Minor"
    elif area_m2 < 0.17:
        return "Moderate"
    return "Severe"


def print_results(metrics: dict, matched: list[dict]) -> None:
    """Print evaluation results in a formatted table."""
    print("\n" + "=" * 60)
    print("SIPAT MODEL EVALUATION RESULTS")
    print("=" * 60)

    print(f"\nGround Truth Samples: {metrics['total_gt']}")
    print(f"Matched Detections:  {metrics['matched']}")
    print(f"Unmatched GT:        {metrics['unmatched_gt']}")

    print("\n--- Area Measurement Accuracy ---")
    print(f"Mean Absolute Error (MAE):     {metrics['mae']:.4f} m²")
    print(f"Root Mean Square Error (RMSE): {metrics['rmse']:.4f} m²")
    print(f"Accuracy (within 20%):         {metrics['accuracy_20_pct']:.1f}%")
    print(f"Accuracy (within 30%):         {metrics['accuracy_30_pct']:.1f}%")

    print("\n--- Severity Classification ---")
    print(f"Severity Accuracy:             {metrics['severity_accuracy_pct']:.1f}%")

    if matched:
        print("\n--- Per-Sample Breakdown ---")
        print(f"{'AI Area (m²)':>14} {'GT Area (m²)':>14} {'Error %':>10} {'Class':>8} {'Sev':>8}")
        print("-" * 60)
        for m in matched[:20]:
            sev = _severity_from_area(m["ai_area_m2"])
            print(f"{m['ai_area_m2']:>14.4f} {m['gt_area_m2']:>14.4f} {m['pct_error']:>9.1f}% {m['ai_class']:>8} {sev:>8}")

    print("\n" + "=" * 60)


def save_results_csv(metrics: dict, matched: list[dict], output_path: str) -> None:
    """Save detailed results to CSV."""
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ai_area_m2", "gt_area_m2", "abs_error", "pct_error",
            "ai_class", "gt_severity", "confidence",
        ])
        for m in matched:
            writer.writerow([
                m["ai_area_m2"], m["gt_area_m2"], m["abs_error"],
                m["pct_error"], m["ai_class"], m["gt_severity"], m["confidence"],
            ])
        writer.writerow([])
        writer.writerow(["metric", "value"])
        writer.writerow(["mae", metrics["mae"]])
        writer.writerow(["rmse", metrics["rmse"]])
        writer.writerow(["accuracy_20_pct", metrics["accuracy_20_pct"]])
        writer.writerow(["accuracy_30_pct", metrics["accuracy_30_pct"]])
        writer.writerow(["severity_accuracy_pct", metrics["severity_accuracy_pct"]])

    print(f"\nResults saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="SIPAT Model Evaluation")
    parser.add_argument("--video", required=True, help="Path to test video")
    parser.add_argument("--ground-truth", required=True, help="Path to ground truth CSV")
    parser.add_argument("--gps", default=None, help="Path to GPS CSV for spatial matching (optional)")
    parser.add_argument("--output", default="evaluation_results.csv", help="Output CSV path")
    parser.add_argument("--match-radius", type=float, default=5.0, help="Match radius in meters")
    args = parser.parse_args()

    print("Loading ground truth...")
    gt = load_ground_truth(args.ground_truth)
    print(f"Loaded {len(gt)} ground truth samples")

    # Load GPS if provided
    gps_processor = None
    if args.ground_truth:
        gps_path = Path(args.ground_truth)
        # Check for GPS CSV in same directory or use --gps flag
        gps_file = args.ground_truth.replace(".csv", "_gps.csv")
        if args.gps:
            gps_file = args.gps
        elif not Path(gps_file).exists():
            gps_file = None
        else:
            gps_file = str(gps_file)

        if gps_file and Path(gps_file).exists():
            print(f"\nLoading GPS data from: {gps_file}")
            import json
            with open(gps_file, "r") as f:
                gps_data = json.load(f)
            gps_processor = GPSProcessor(gps_data)
            if gps_processor.is_stationary():
                print("Warning: GPS track is stationary, collapsing to median coordinate")
                gps_data = gps_processor.collapse_to_median()
                gps_processor = GPSProcessor(gps_data)
            print(f"GPS loaded: {len(gps_data)} points")

    print("\nRunning YOLO + IPM detection...")
    detections = run_detection(args.video, gps_processor)

    print("\nMatching detections to ground truth...")
    matched = match_detections_to_ground_truth(detections, gt, args.match_radius)

    metrics = compute_metrics(matched, len(gt))
    print_results(metrics, matched)
    save_results_csv(metrics, matched, args.output)


if __name__ == "__main__":
    main()
