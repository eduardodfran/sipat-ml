import os
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent.parent

# ---- YOLO model ----
MODEL_PATH = CURRENT_DIR.parent / "weights" / "best.pt"
YOLO_CONFIDENCE = float(os.getenv("SIPAT_YOLO_CONFIDENCE", "0.20"))
COMMUNITY_PHOTO_YOLO_CONFIDENCE = float(os.getenv("SIPAT_COMMUNITY_PHOTO_YOLO_CONFIDENCE", "0.20"))
_IOU_THRESHOLD = 0.7
FRAME_SKIP = 5

# ---- frame quality filters ----
# Laplacian variance below this = too blurry for reliable detection
BLUR_THRESHOLD = 80.0
# Mean brightness below this = too dark for reliable detection
DARK_THRESHOLD = 30.0

# ---- frame cropping ----
# RDD2022 was trained on forward-facing vehicle cameras where the road fills
# most of the frame. Portrait phone video puts the road in the bottom ~50%.
# CROP_TOP_RATIO = 0.0 means no crop; 0.4 means cut top 40% off.
CROP_TOP_RATIO = 0.4

# ---- class filtering ----
# Road marking classes (D43 = crosswalk blur, D44 = white line blur) are not
# pavement distress and produce false positives on normal road features.
EXCLUDED_CLASSES: set[str] = {"D43", "D44"}

# ---- clustering & merge radius ----
MERGE_RADIUS_METERS = 10.0
CLUSTER_MIN_DETECTIONS = 3

# ---- DPWH-aligned severity thresholds (FHWA LTPP / PAVER derived) ----
# DPWH D.O. No. 120 s. 2019 adopts FHWA LTPP Distress ID Manual for pothole
# severity, which classifies by depth (<25mm Low, 25-50mm Moderate, >50mm High).
# Since only plan area is available, map via PAVER (US Army) combined diameter/depth
# matrix: 200mm diam (~0.03m^2) and 460mm diam (~0.17m^2) boundaries.
# Source: FHWA-RD-03-031 LTPP Distress ID Manual §8 Potholes;
#         PAVER Road Asphalt Distress Manual §13 Potholes Table 1.
SEVERITY_MINOR_AREA_M2 = 0.03
SEVERITY_MODERATE_AREA_M2 = 0.17

# ---- confidence-based severity capping ----
CONFIDENCE_MODERATE_CAP = 0.25
CONFIDENCE_SEVERE_CAP = 0.50

# ---- frame-area heuristic severity thresholds ----
FRAME_SEVERITY_MINOR_PCT = 2.0   # bbox area < 2% of frame → Minor
FRAME_SEVERITY_SEVERE_PCT = 6.0  # bbox area > 6% of frame → Severe

# ---- IPM defaults ----
DEFAULT_NEAR_METERS = 3.0
DEFAULT_FAR_METERS = 25.0
DEFAULT_ROAD_WIDTH_METERS = 6.0
DEFAULT_PIXELS_PER_METER = 100.0

# ---- storage ----
ANNOTATED_FRAMES_BUCKET = "detected-images"
RAW_DATA_BUCKET = "raw-road-data"

# ---- concurrency ----
MAX_WORKERS = int(os.getenv("SIPAT_ML_MAX_WORKERS", "2"))
WORKERS = int(os.getenv("SIPAT_ML_WORKERS", "1"))
WORKER_TIMEOUT = int(os.getenv("SIPAT_ML_WORKER_TIMEOUT", "120"))
MAX_CONCURRENT_RIDES = int(os.getenv("SIPAT_ML_MAX_CONCURRENT_RIDES", "4"))
RIDE_PROCESS_TIMEOUT = int(os.getenv("SIPAT_ML_RIDE_TIMEOUT", "600"))
BATCH_INSERT_SIZE = int(os.getenv("SIPAT_ML_BATCH_INSERT_SIZE", "500"))

# ---- connection pool ----
SUPABASE_POOL_SIZE = int(os.getenv("SIPAT_ML_SUPABASE_POOL_SIZE", "10"))
AZURE_MAX_CONNECTIONS = int(os.getenv("SIPAT_ML_AZURE_MAX_CONNECTIONS", "10"))
MAX_VIDEO_SIZE_MB = int(os.getenv("SIPAT_ML_MAX_VIDEO_SIZE_MB", "2048"))

# ---- geo constants ----
EARTH_RADIUS_METERS = 6371008.8
STATIONARY_THRESHOLD_METERS = 12.0
