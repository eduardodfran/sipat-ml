-- Fix: Backfill image_url + detectors for existing potholes, and fix the view
-- TICKET-DETECT-003/004: Images not showing + no detector data for existing records

-- Haversine distance helper (inline, no extension needed)
CREATE OR REPLACE FUNCTION _hap_distance(
    lat1 DOUBLE PRECISION, lng1 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION, lng2 DOUBLE PRECISION
) RETURNS DOUBLE PRECISION
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN 6371008.8 * 2 * ASIN(
        SQRT(
            POWER(SIN(RADIANS(lat2 - lat1) / 2), 2) +
            COS(RADIANS(lat1)) * COS(RADIANS(lat2)) *
            POWER(SIN(RADIANS(lng2 - lng1) / 2), 2)
        )
    );
END;
$$;

-- Step 1: Backfill image_url on verified_potholes from raw_detections
UPDATE verified_potholes vp
SET image_url = sub.image_url
FROM (
    SELECT DISTINCT ON (rd_inner.pothole_id)
        rd_inner.pothole_id,
        rd_inner.image_url
    FROM (
        SELECT
            vp2.id AS pothole_id,
            rd.image_url,
            _hap_distance(vp2.consolidated_latitude, vp2.consolidated_longitude, rd.lat, rd.lng) AS dist
        FROM verified_potholes vp2
        JOIN raw_detections rd ON
            _hap_distance(vp2.consolidated_latitude, vp2.consolidated_longitude, rd.lat, rd.lng) <= 15.0  -- must match MERGE_RADIUS_METERS in batch_worker.py
        WHERE (vp2.image_url IS NULL OR vp2.image_url = '')
        AND rd.image_url IS NOT NULL
    ) rd_inner
    ORDER BY rd_inner.pothole_id, rd_inner.dist ASC
) sub
WHERE vp.id = sub.pothole_id
AND (vp.image_url IS NULL OR vp.image_url = '');

-- Step 2: Backfill user_detections on verified_potholes from raw_detections
UPDATE verified_potholes vp
SET user_detections = sub.detections
FROM (
    SELECT
        vp2.id AS pothole_id,
        COALESCE(
            jsonb_agg(
                DISTINCT jsonb_build_object(
                    'user_id', rd.user_id,
                    'video_timestamp', rd.video_timestamp
                )
            ) FILTER (WHERE rd.user_id IS NOT NULL),
            '[]'::jsonb
        ) AS detections
    FROM verified_potholes vp2
    JOIN raw_detections rd ON
        _hap_distance(vp2.consolidated_latitude, vp2.consolidated_longitude, rd.lat, rd.lng) <= 15.0  -- must match MERGE_RADIUS_METERS in batch_worker.py
    WHERE (vp2.user_detections IS NULL OR vp2.user_detections = '[]'::jsonb)
    GROUP BY vp2.id
) sub
WHERE vp.id = sub.pothole_id
AND (vp.user_detections IS NULL OR vp.user_detections = '[]'::jsonb);

-- Step 3: Recreate get_pothole_detectors RPC (safe TEXT cast, uses _hap_distance)
-- Drop ALL possible overloads to avoid return-type conflicts
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision, double precision);
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision);
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision, double precision, text);
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision, text);

CREATE OR REPLACE FUNCTION get_pothole_detectors(
    p_lat DOUBLE PRECISION,
    p_lng DOUBLE PRECISION,
    radius_meters DOUBLE PRECISION DEFAULT 15.0
)
RETURNS TABLE (
    user_id TEXT,
    username TEXT,
    full_name TEXT,
    detected_at TIMESTAMPTZ
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        rd.user_id::TEXT,
        p.username::TEXT,
        p.full_name::TEXT,
        MIN((rm.created_at + (rd.video_timestamp || ' seconds')::INTERVAL)::TIMESTAMPTZ) AS detected_at
    FROM raw_detections rd
    JOIN rides_metadata rm ON rm.id = rd.ride_id
    LEFT JOIN profiles p ON p.id::TEXT = rd.user_id::TEXT
    WHERE rd.user_id IS NOT NULL
        AND _hap_distance(rd.lat, rd.lng, p_lat, p_lng) <= radius_meters
    GROUP BY rd.user_id, p.username, p.full_name;
END;
$$;

-- Step 4: Recreate the view with proper fallback for image_url
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
