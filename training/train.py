"""
SIPAT YOLO Training Script

Fine-tunes YOLOv8n on RDD2022 (Road Damage Detection 2022) dataset
for Philippine urban road distress detection.

Dataset: RDD2022 - Road Damage Detection Challenge 2022
  - Source: https://github.com/sekilab/RoadDamageDetector
  - Classes: D00 (longitudinal crack), D10 (transverse crack),
             D20 (patching), D40 (pothole), D43 (crosswalk blur),
             D44 (white line blur)
  - Custom subset: Philippine urban roads from Metro Manila

Usage:
    python train.py --data dataset.yaml --epochs 100 --model yolov8n.pt
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO


# ---- Default Training Configuration ----

DEFAULT_CONFIG = {
    # Model
    "model": "yolov8n.pt",       # YOLOv8 nano (3.2M params, lightweight)
    "task": "detect",

    # Data
    "data": "dataset.yaml",       # RDD2022 YAML config

    # Hyperparameters
    "epochs": 100,
    "batch": 16,
    "imgsz": 640,
    "lr0": 0.01,                  # Initial learning rate
    "lrf": 0.01,                  # Final learning rate factor
    "momentum": 0.937,
    "weight_decay": 0.0005,
    "warmup_epochs": 3.0,
    "warmup_momentum": 0.8,

    # Augmentation
    "hsv_h": 0.015,               # HSV-Hue augmentation
    "hsv_s": 0.7,                 # HSV-Saturation augmentation
    "hsv_v": 0.4,                 # HSV-Value augmentation
    "degrees": 0.0,               # Rotation (0 = no rotation for road)
    "translate": 0.1,             # Translation
    "scale": 0.5,                 # Scale augmentation
    "fliplr": 0.5,                # Horizontal flip
    "mosaic": 1.0,                # Mosaic augmentation
    "mixup": 0.0,                 # No mixup (not needed for road)
    "copy_paste": 0.0,            # No copy-paste

    # Efficiency
    "workers": 8,
    "patience": 20,               # Early stopping patience
    "cache": True,                # Cache images for faster training

    # Output
    "project": "runs/detect",
    "name": "sipat_v1",
}


def create_dataset_yaml(output_path: str = "dataset.yaml") -> None:
    """Create the RDD2022 dataset YAML configuration."""
    yaml_content = """# SIPAT Dataset Configuration
# RDD2022 - Road Damage Detection 2022 (Philippine subset)
# Modified from: https://github.com/sekilab/RoadDamageDetector

path: ../datasets/rdd2022   # Dataset root directory
train: train/images           # Train images (relative to path)
val: val/images               # Val images (relative to path)
test: test/images             # Test images (relative to path)

# Class names (RDD2022 taxonomy)
names:
  0: D00   # Longitudinal crack
  1: D10   # Transverse crack
  2: D20   # Patching
  3: D40   # Pothole
  4: D43   # Crosswalk blur (excluded in inference)
  5: D44   # White line blur (excluded in inference)

# Number of classes
nc: 6
"""
    Path(output_path).write_text(yaml_content)
    print(f"Created dataset config: {output_path}")


def train_model(config: dict) -> None:
    """Run YOLO training with given configuration."""
    print("\n" + "=" * 60)
    print("SIPAT YOLO Training")
    print("=" * 60)

    print(f"\nModel:      {config['model']}")
    print(f"Dataset:    {config['data']}")
    print(f"Epochs:     {config['epochs']}")
    print(f"Batch Size: {config['batch']}")
    print(f"Image Size: {config['imgsz']}")
    print(f"LR:         {config['lr0']}")
    print(f"Patience:   {config['patience']}")

    model = YOLO(config["model"])

    results = model.train(
        data=config["data"],
        epochs=config["epochs"],
        batch=config["batch"],
        imgsz=config["imgsz"],
        lr0=config["lr0"],
        lrf=config["lrf"],
        momentum=config["momentum"],
        weight_decay=config["weight_decay"],
        warmup_epochs=config["warmup_epochs"],
        warmup_momentum=config["warmup_momentum"],
        hsv_h=config["hsv_h"],
        hsv_s=config["hsv_s"],
        hsv_v=config["hsv_v"],
        degrees=config["degrees"],
        translate=config["translate"],
        scale=config["scale"],
        fliplr=config["fliplr"],
        mosaic=config["mosaic"],
        mixup=config["mixup"],
        copy_paste=config["copy_paste"],
        workers=config["workers"],
        patience=config["patience"],
        cache=config["cache"],
        project=config["project"],
        name=config["name"],
    )

    print("\nTraining complete!")
    print(f"Results saved to: {config['project']}/{config['name']}")
    return results


def evaluate_model(model_path: str, data_yaml: str) -> None:
    """Evaluate trained model on validation set."""
    print("\nEvaluating model...")
    model = YOLO(model_path)
    metrics = model.val(data=data_yaml)

    print("\n--- Validation Metrics ---")
    print(f"mAP50:      {metrics.box.map50:.4f}")
    print(f"mAP50-95:   {metrics.box.map:.4f}")
    print(f"Precision:  {metrics.box.mp:.4f}")
    print(f"Recall:     {metrics.box.mr:.4f}")
    return metrics


def export_model(model_path: str) -> None:
    """Export trained model to various formats."""
    print("\nExporting model...")
    model = YOLO(model_path)

    # Export to ONNX (for deployment)
    model.export(format="onnx", imgsz=640)
    print("Exported to ONNX format")


def main():
    parser = argparse.ArgumentParser(description="SIPAT YOLO Training")
    parser.add_argument("--data", default="dataset.yaml", help="Dataset YAML path")
    parser.add_argument("--model", default="yolov8n.pt", help="Base model path")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs")
    parser.add_argument("--batch", type=int, default=16, help="Batch size")
    parser.add_argument("--imgsz", type=int, default=640, help="Image size")
    parser.add_argument("--lr", type=float, default=0.01, help="Learning rate")
    parser.add_argument("--patience", type=int, default=20, help="Early stopping patience")
    parser.add_argument("--project", default="runs/detect", help="Output directory")
    parser.add_argument("--name", default="sipat_v1", help="Experiment name")
    parser.add_argument("--eval-only", action="store_true", help="Only evaluate")
    parser.add_argument("--create-dataset", action="store_true", help="Create dataset YAML")
    args = parser.parse_args()

    if args.create_dataset:
        create_dataset_yaml(args.data)
        return

    config = DEFAULT_CONFIG.copy()
    config.update({
        "model": args.model,
        "data": args.data,
        "epochs": args.epochs,
        "batch": args.batch,
        "imgsz": args.imgsz,
        "lr0": args.lr,
        "patience": args.patience,
        "project": args.project,
        "name": args.name,
    })

    if args.eval_only:
        model_path = f"{args.project}/{args.name}/weights/best.pt"
        evaluate_model(model_path, args.data)
    else:
        train_model(config)

        # Evaluate after training
        model_path = f"{args.project}/{args.name}/weights/best.pt"
        evaluate_model(model_path, args.data)


if __name__ == "__main__":
    main()
