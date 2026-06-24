-- Atomic fix: creates _hap_distance + get_pothole_detectors from scratch
-- Run this ONE block in Supabase SQL Editor

-- 1. Haversine distance helper (safe to re-run)
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

-- 2. Drop ALL overloads of get_pothole_detectors
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision, double precision);
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision);

-- 3. Recreate with TEXT-safe casts (cast BOTH sides to TEXT to avoid type mismatch)
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

-- 4. Reload Supabase schema cache
NOTIFY pgrst, 'reload schema';

GRANT EXECUTE ON FUNCTION _hap_distance(double precision, double precision, double precision, double precision) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_pothole_detectors(double precision, double precision, double precision) TO anon, authenticated;
