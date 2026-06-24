-- Fix: Drop ALL overloads of get_pothole_detectors and recreate with safe TEXT types
-- The migration ordering left a stale version with text=uuid comparison errors

DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision, double precision);
DROP FUNCTION IF EXISTS get_pothole_detectors(double precision, double precision);

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

GRANT EXECUTE ON FUNCTION get_pothole_detectors(double precision, double precision, double precision) TO anon, authenticated;
