-- Migration: Add caption to verified_potholes + update view
-- Auto-generated description for detected potholes, editable by users

-- Step 1: Add caption column
ALTER TABLE verified_potholes ADD COLUMN IF NOT EXISTS caption TEXT;

-- Step 2: Recreate v_unified_potholes with caption
DROP VIEW IF EXISTS v_unified_potholes CASCADE;

CREATE OR REPLACE VIEW v_unified_potholes AS
SELECT
    vp.id AS pothole_id,
    vp.consolidated_latitude,
    vp.consolidated_longitude,
    vp.worst_severity,
    vp.total_detection_hits,
    COALESCE(
        vp.image_url,
        (
            SELECT rd.image_url
            FROM raw_detections rd
            JOIN rides_metadata rm ON rm.id = rd.ride_id
            WHERE rd.image_url IS NOT NULL
                AND _hap_distance(vp.consolidated_latitude, vp.consolidated_longitude, rd.lat, rd.lng) <= 15.0
            ORDER BY (rm.created_at + (rd.video_timestamp || ' seconds')::INTERVAL) DESC
            LIMIT 1
        )
    ) AS image_url,
    vp.caption,
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
    ) AS reporter_avatar,
    vp.street,
    vp.barangay,
    vp.city,
    vp.province,
    vp.region,
    vp.country,
    vp.formatted_address,
    vp.address_geocoded_at
FROM verified_potholes vp;
