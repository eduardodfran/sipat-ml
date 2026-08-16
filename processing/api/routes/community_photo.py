from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from ...config.settings import MODEL_PATH, COMMUNITY_PHOTO_YOLO_CONFIDENCE, EXCLUDED_CLASSES
from ...core.severity import frame_area_pct_to_severity
from ...rate_limiter import UPLOAD_LIMIT, limiter
from ...services.geocoder import reverse_geocode
from ...services.supabase_client import get_supabase_service, PGRST204Error

logger = logging.getLogger(__name__)
router = APIRouter(tags=["community-photo"])

COMMUNITY_PHOTO_COLUMNS = {
    "id", "user_id", "image_url", "latitude", "longitude",
    "street", "barangay", "city", "province", "region", "country",
    "formatted_address", "address_geocoded_at", "detection_status",
    "worst_severity", "confidence", "class_name", "caption",
    "created_at", "updated_at",
}

_shared_model = None


def _get_yolo_model():
    global _shared_model
    if _shared_model is None:
        from ultralytics import YOLO
        if not MODEL_PATH.exists():
            return None
        _shared_model = YOLO(str(MODEL_PATH))
    return _shared_model


def _run_yolo_on_image(image_path: str) -> dict:
    try:
        model = _get_yolo_model()
        if model is None:
            logger.warning("YOLO model not found at %s", MODEL_PATH)
            return {}

        results = model(image_path, conf=COMMUNITY_PHOTO_YOLO_CONFIDENCE, verbose=False)

        if not results or len(results) == 0:
            return {}

        r = results[0]
        if r.boxes is None or len(r.boxes) == 0:
            return {}

        best_detection = None
        highest_conf = -1.0

        for i in range(len(r.boxes)):
            conf = float(r.boxes.conf[i])
            cls_id = int(r.boxes.cls[i])
            class_name = r.names.get(cls_id, f"class_{cls_id}")

            if class_name in EXCLUDED_CLASSES:
                continue

            if conf > highest_conf:
                highest_conf = conf
                best_detection = {
                    "confidence": conf,
                    "class_name": class_name,
                    "class_id": cls_id,
                    "bbox": r.boxes[i].xyxyn[0].tolist(),
                }

        return best_detection or {}
    except Exception as exc:
        logger.warning("YOLO inference failed: %s", exc)
        return {}


@router.post("/community-photo/upload")
@limiter.limit(UPLOAD_LIMIT)
async def upload_community_photo(
    request: Request,
    image: UploadFile = File(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    caption: str = Form(default=None),
    authorization: str = Header(None),
):
    svc = get_supabase_service()
    auth = svc.validate_token(authorization)
    user_id = auth.get("user_id") or auth.get("sub")

    reporter_username = None
    reporter_avatar = None
    if user_id:
        try:
            profiles = svc.select("profiles", "username, avatar_url", id=user_id)
            if profiles:
                reporter_username = profiles[0].get("username")
                reporter_avatar = profiles[0].get("avatar_url")
        except Exception as exc:
            logger.warning("Could not fetch profile for %s: %s", user_id, exc)

    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    image_bytes = await image.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10MB)")
    filename = f"community/{uuid.uuid4().hex}.jpg"

    try:
        svc.storage_upload(
            bucket="community-photos",
            path=filename,
            file_bytes=image_bytes,
            content_type="image/jpeg",
            upsert=False,
        )
        image_url = svc.storage_get_public_url("community-photos", filename)
    except Exception as exc:
        logger.error("Storage upload failed: %s", exc)
        raise HTTPException(500, "Failed to upload image")

    addr = await run_in_threadpool(reverse_geocode, latitude, longitude)

    photo_data = {
        "user_id": user_id,
        "image_url": image_url,
        "latitude": latitude,
        "longitude": longitude,
        "reporter_username": reporter_username,
        "reporter_avatar": reporter_avatar,
        "caption": caption if caption else None,
        "street": addr.get("street") if addr else None,
        "barangay": addr.get("barangay") if addr else None,
        "city": addr.get("city") if addr else None,
        "province": addr.get("province") if addr else None,
        "region": addr.get("region") if addr else None,
        "country": addr.get("country") if addr else None,
        "formatted_address": addr.get("formatted_address") if addr else None,
        "address_geocoded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if addr else None,
        "detection_status": "pending",
    }

    photo_data = {k: v for k, v in photo_data.items() if k in COMMUNITY_PHOTO_COLUMNS}

    import re
    max_insert_retries = 5
    for attempt in range(max_insert_retries):
        try:
            result = svc.insert("community_photos", photo_data)
            photo_id = result.data[0]["id"]
            break
        except PGRST204Error as exc:
            missing_cols = re.findall(r"Could not find the '(\w+)' column", str(exc))
            for col in missing_cols:
                logger.warning("column %s missing from community_photos, stripping it", col)
                photo_data.pop(col, None)
            if attempt < max_insert_retries - 1:
                continue
            logger.error("DB insert failed after %d retries: %s", max_insert_retries, exc)
            raise HTTPException(500, "Failed to save photo record")
        except Exception as exc:
            logger.error("DB insert failed: %s", exc)
            raise HTTPException(500, "Failed to save photo record")

    # Auto-expire old stuck pending posts (>1 hour) so they don't show forever
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        svc.client.table("community_photos") \
            .update({"detection_status": "no_detection"}) \
            .eq("detection_status", "pending") \
            .lt("created_at", cutoff) \
            .execute()
    except Exception as exc:
        logger.warning("Stale pending cleanup failed (non-critical): %s", exc)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        detection = await run_in_threadpool(_run_yolo_on_image, tmp_path)

        if detection:
            severity = "Minor"
            try:
                severity = frame_area_pct_to_severity(detection.get("bbox", [0, 0, 0.1, 0.1]))
            except Exception:
                pass

            svc.update(
                "community_photos",
                {
                    "detection_status": "processed",
                    "worst_severity": severity,
                    "confidence": detection["confidence"],
                    "class_name": detection["class_name"],
                },
                id=photo_id,
            )
        else:
            svc.update(
                "community_photos",
                {"detection_status": "no_detection"},
                id=photo_id,
            )
    finally:
        os.unlink(tmp_path)

    return {
        "photo_id": photo_id,
        "image_url": image_url,
    }
