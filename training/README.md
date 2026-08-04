# SIPAT Model Training

## Overview

SIPAT uses a fine-tuned YOLOv8n (nano) model for road distress detection. The model is trained on the RDD2022 dataset with Philippine urban road samples.

## Model Architecture

- **Base Model:** YOLOv8n (Nano) — 3.2M parameters, lightweight
- **Input:** 640×640 RGB images
- **Output:** Bounding boxes with class labels and confidence scores
- **Classes:** D00, D10, D20, D40 (D43, D44 excluded in inference)

## Dataset

### RDD2022 (Road Damage Detection 2022)

- **Source:** https://github.com/sekilab/RoadDamageDetector
- **Total Images:** ~26,000 (global), ~1,000+ (Philippine subset)
- **Classes:**

| Code | Description | Typical Size |
|------|-------------|--------------|
| D00 | Longitudinal crack | 5-50 cm width |
| D10 | Transverse crack | 5-50 cm length |
| D20 | Patching | 10-100 cm diameter |
| D40 | Pothole | 10-80 cm diameter |
| D43 | Crosswalk blur | (excluded) |
| D44 | White line blur | (excluded) |

### Data Preparation

1. Download RDD2022 dataset from GitHub
2. Extract Philippine urban road subset
3. Convert annotations to YOLO format:
   ```
   class_id center_x center_y width height
   ```
4. Split into train/val/test (80/10/10)
5. Create `dataset.yaml`:
   ```yaml
   path: ../datasets/rdd2022
   train: train/images
   val: val/images
   names:
     0: D00
     1: D10
     2: D20
     3: D40
     4: D43
     5: D44
   ```

## Training Configuration

### Hyperparameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Epochs | 100 | Sufficient for convergence |
| Batch Size | 16 | Fits in 8GB GPU memory |
| Image Size | 640×640 | YOLOv8 default |
| Learning Rate | 0.01 | Standard for fine-tuning |
| Early Stopping | 20 epochs | Prevents overfitting |

### Augmentation

| Augmentation | Value | Purpose |
|--------------|-------|---------|
| HSV-Hue | 0.015 | Color variation |
| HSV-Saturation | 0.7 | Lighting variation |
| HSV-Value | 0.4 | Brightness variation |
| Translation | 0.1 | Position variation |
| Scale | 0.5 | Size variation |
| Mosaic | 1.0 | Context augmentation |

### Why These Choices?

1. **YOLOv8n (Nano):** Lightweight for mobile/edge deployment, fast inference
2. **No rotation:** Road images have consistent orientation
3. **No mixup:** Not needed for road distress (objects are small)
4. **Mosaic enabled:** Helps detect small potholes by combining contexts

## Training Commands

### Quick Start

```bash
cd sipat-ml
python training/train.py --create-dataset
python training/train.py --data dataset.yaml --epochs 100
```

### Custom Training

```bash
python training/train.py \
  --data dataset.yaml \
  --model yolov8n.pt \
  --epochs 100 \
  --batch 16 \
  --imgsz 640 \
  --lr 0.01 \
  --patience 20 \
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

1. **YOLOv8:** https://github.com/ultralytics/ultralytics
2. **RDD2022:** https://github.com/sekilab/RoadDamageDetector
3. **RDD2022 Paper:** "Road Damage Detection and Classification with YOLOv5 and EfficientDet" (2022)
