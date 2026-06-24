-- Migration: Add user_detections to verified_potholes + create get_pothole_detectors RPC
-- TICKET-DETECT-001: Multi-user detection tracking with timestamps

-- Step 1: Add user_detections JSONB column to verified_potholes
-- Stores array of {user_id, username, detected_at} per pothole
ALTER TABLE verified_potholes
ADD COLUMN IF NOT EXISTS user_detections JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Step 2: Index on lat/lng for raw_detections for faster proximity queries
CREATE INDEX IF NOT EXISTS idx_raw_detections_lat_lng
ON raw_detections (lat, lng);

-- Step 3: Create get_pothole_detectors RPC function
-- Returns all unique users who detected a pothole within radius_meters of (p_lat, p_lng)
-- Each row: user_id, username, full_name, and earliest detection timestamp
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision, double precision);
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision);

CREATE OR REPLACE FUNCTION get_pothole_detectors(
    p_lat DOUBLE PRECISION,
    p_lng DOUBLE PRECISION,
    radius_meters DOUBLE PRECISION DEFAULT 15.0
)
RETURNS TABLE (
    user_id UUID,
    username TEXT,
    full_name TEXT,
    detected_at TIMESTAMPTZ
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT
        rd.user_id::UUID,
        p.username::TEXT,
        p.full_name::TEXT,
        MIN((rm.created_at + (rd.video_timestamp || ' seconds')::INTERVAL)::TIMESTAMPTZ) AS detected_at
    FROM raw_detections rd
    JOIN rides_metadata rm ON rm.id = rd.ride_id
    LEFT JOIN profiles p ON p.id = rd.user_id::UUID
    WHERE (
        6371008.8 * 2 * ASIN(
            SQRT(
                POWER(SIN(RADIANS(rd.lat - p_lat) / 2), 2) +
                COS(RADIANS(p_lat)) * COS(RADIANS(rd.lat)) *
                POWER(SIN(RADIANS(rd.lng - p_lng) / 2), 2)
            )
        )
    ) <= radius_meters
    GROUP BY rd.user_id, p.username, p.full_name;
END;
$$;
