-- Migration: Add detectors_count to v_unified_potholes view
-- TICKET-DETECT-004: Shows "Detected by N people" on web dashboard

DROP VIEW IF EXISTS v_unified_potholes CASCADE;

CREATE OR REPLACE VIEW v_unified_potholes AS
SELECT
    vp.id AS pothole_id,
    vp.consolidated_latitude,
    vp.consolidated_longitude,
    vp.worst_severity,
    vp.total_detection_hits,
    vp.image_url,
    vp.updated_at AS latest_activity_at,
    COALESCE(jsonb_array_length(vp.user_detections), 0) AS detectors_count,
    (
        SELECT rm.created_at
        FROM rides_metadata rm
        WHERE rm.id = vp.ride_id
        ORDER BY rm.created_at ASC
        LIMIT 1
    ) AS citizen_first_reported_at,
    (
        SELECT p.username
        FROM rides_metadata rm
        JOIN profiles p ON p.id = rm.user_id
        WHERE rm.id = vp.ride_id
        ORDER BY rm.created_at ASC
        LIMIT 1
    ) AS reporter_username,
    (
        SELECT p.avatar_url
        FROM rides_metadata rm
        JOIN profiles p ON p.id = rm.user_id
        WHERE rm.id = vp.ride_id
        ORDER BY rm.created_at ASC
        LIMIT 1
    ) AS reporter_avatar
FROM verified_potholes vp;
