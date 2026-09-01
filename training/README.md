# SIPAT Model Training

## Overview

SIPAT uses a fine-tuned YOLOv26n (nano) model for road distress detection. The model is trained on the RDD2022 dataset (38,383 images, 16 classes) exported from Roboflow.

## Model Architecture

- **Base Model:** YOLOv26n (Nano)
- **Input:** 640×640 RGB images
- **Output:** Bounding boxes with class labels and confidence scores
- **Classes:** 16 classes including D00, D10, D20, D40, D43, D44, plus rotation variants and additional classes

## Dataset

### RDD2022 (Road Damage Detection 2022)

- **Source:** https://universe.roboflow.com/aleciofvjunior/rdd2022-zw12e
- **Total Images:** 38,383
- **Format:** YOLOv8
- **Pre-processing:** Auto-orientation (EXIF-orientation stripping) + Resize to 640×640 (Stretch)
- **Augmentation:** None applied
- **Classes (16):**

| Code | Description |
|------|-------------|
| Block crack | Block-shaped crack pattern |
| D00 | Longitudinal crack |
| D00rotation | Longitudinal crack (rotated) |
| D01 | Longitudinal crack variant |
| D0w0 | Longitudinal crack variant |
| D10 | Transverse crack |
| D10rotation | Transverse crack (rotated) |
| D11 | Transverse crack variant |
| D20 | Patching |
| D20rotation | Patching (rotated) |
| D40 | Pothole |
| D40rotation | Pothole (rotated) |
| D43 | Crosswalk blur |
| D44 | White line blur |
| D50 | Non-rated distress |
| Repair | Repaired surface |

### Data Preparation

1. Export RDD2022 dataset from Roboflow (https://app.roboflow.com/aleciofvjunior/rdd2022-zw12e/2)
2. Dataset structure:
   ```
   RDD2022-2/
   ├── train/images/
   ├── valid/images/
   ├── test/images/
   └── data.yaml
   ```
3. Annotations in YOLOv8 format:
   ```
   class_id center_x center_y width height
   ```

## Training Configuration

### Hyperparameters (from model checkpoint)

| Parameter | Value | Source |
|-----------|-------|--------|
| Epochs | 80 | model train_args |
| Batch Size | 32 | model train_args |
| Image Size | 640×640 | model train_args |
| Learning Rate | 0.0054 | model train_args |
| Early Stopping | 25 epochs | model train_args |
| Optimizer | SGD | model train_args |
| Device | Kaggle GPU | model train_args |

### Training Platform

- **Platform:** Kaggle
- **Training Date:** 2026-06-27
- **Output Path:** `/kaggle/working/SIPAT_Training/sipat_yolo26n_production`

### Why These Choices?

1. **YOLOv26n (Nano):** Lightweight for mobile/edge deployment, fast inference
2. **No augmentation:** Dataset already contains rotation variants covering orientation diversity
3. **Batch size 32:** Maximizes GPU memory utilization on Kaggle

## Training Commands

### Quick Start

```bash
cd sipat-ml
python training/train.py --create-dataset
python training/train.py --data dataset.yaml --epochs 80
```

### Custom Training

```bash
python training/train.py \
  --data dataset.yaml \
  --model yolo26n.pt \
  --epochs 80 \
  --batch 32 \
  --imgsz 640 \
  --lr 0.0054 \
  --patience 25 \
  --project runs/detect \
  --name sipat_v1
```

### Evaluate Only

```bash
python training/train.py --eval-only --project runs/detect --name sipat_v1
```

## Output

Training results are saved to:
```
runs/detect/sipat_v1/
├── weights/
│   ├── best.pt          # Best model (by mAP50)
│   └── last.pt          # Last epoch checkpoint
├── results.csv          # Training metrics per epoch
├── results.png          # Training curves
├── confusion_matrix.png # Confusion matrix
├── F1_curve.png         # F1-score curve
├── PR_curve.png         # Precision-Recall curve
├── P_curve.png          # Precision curve
└── R_curve.png          # Recall curve
```

## Evaluation Metrics

| Metric | Target | Description |
|--------|--------|-------------|
| mAP50 | >0.5 | Mean Average Precision at IoU=0.5 |
| mAP50-95 | >0.3 | Mean Average Precision at IoU=0.5:0.95 |
| Precision | >0.6 | True Positives / (True Positives + False Positives) |
| Recall | >0.5 | True Positives / (True Positives + False Negatives) |

## Model Deployment

After training, copy `best.pt` to:
```
sipat-ml/weights/best.pt
```

This is the model used by the inference pipeline.

## References

1. **YOLOv26:** https://github.com/ultralytics/ultralytics
2. **RDD2022 Dataset:** https://universe.roboflow.com/aleciofvjunior/rdd2022-zw12e
3. **RDD2022 Paper:** "Road Damage Detection and Classification with YOLOv5 and EfficientDet" (2022)
