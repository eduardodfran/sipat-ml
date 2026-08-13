from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from ...config.settings import MODEL_PATH, COMMUNITY_PHOTO_YOLO_CONFIDENCE, EXCLUDED_CLASSES
from ...rate_limiter import UPLOAD_LIMIT, limiter
from ...services.geocoder import reverse_geocode
from ...services.supabase_client import get_supabase_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["community-photo"])

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
                }

        return best_detection or {}
    except Exception as exc:
        logger.warning("YOLO inference failed: %s", exc)
        return {}


def _classify_severity(confidence: float) -> str:
    if confidence >= 0.7:
        return "Severe"
    elif confidence >= 0.4:
        return "Moderate"
    return "Minor"


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

    try:
        result = svc.insert("community_photos", photo_data)
        photo_id = result.data[0]["id"]
    except Exception as exc:
        err_str = str(exc)
        if "PGRST204" in err_str and "caption" in err_str:
            logger.warning("caption column missing, retrying without it")
            photo_data.pop("caption", None)
            try:
                result = svc.insert("community_photos", photo_data)
                photo_id = result.data[0]["id"]
            except Exception as exc2:
                logger.error("DB insert failed (no caption): %s", exc2)
                raise HTTPException(500, "Failed to save photo record")
        else:
            logger.error("DB insert failed: %s", exc)
            raise HTTPException(500, "Failed to save photo record")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        detection = await run_in_threadpool(_run_yolo_on_image, tmp_path)

        if detection:
            svc.update(
                "community_photos",
                {
                    "detection_status": "processed",
                    "worst_severity": _classify_severity(detection.get("confidence", 0)),
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
