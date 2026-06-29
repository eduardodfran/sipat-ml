-- Migration: Add distress summary RPC for dashboard analytics
-- Returns per-class detection stats from raw_detections (excludes road marking blur)

CREATE OR REPLACE FUNCTION get_distress_summary()
RETURNS TABLE (
    class_name        TEXT,
    detection_count   INTEGER,
    avg_confidence    DOUBLE PRECISION,
    worst_severity    TEXT,
    sample_image_url  TEXT
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        rd.class_name::TEXT,
        COUNT(*)::INTEGER,
        ROUND(AVG(rd.confidence)::NUMERIC, 4)::DOUBLE PRECISION,
        MAX(rd.severity)::TEXT,
        (
            SELECT rd2.image_url
            FROM raw_detections rd2
            WHERE rd2.class_name = rd.class_name
              AND rd2.image_url IS NOT NULL
            ORDER BY rd2.confidence DESC
            LIMIT 1
        )::TEXT
    FROM raw_detections rd
    WHERE rd.class_name IS NOT NULL
      AND rd.class_name NOT IN ('D43', 'D44')
    GROUP BY rd.class_name
    ORDER BY COUNT(*) DESC;
END;
$$;

GRANT EXECUTE ON FUNCTION get_distress_summary() TO anon, authenticated;
