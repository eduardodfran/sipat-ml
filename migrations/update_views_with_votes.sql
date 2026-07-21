-- Migration: Update views with vote counts + hot score algorithm
-- Adds hot_score calculation, vote/report counts to views, and feed functions

-- =============================================================================
-- 1. calculate_hot_score function
-- =============================================================================
DROP FUNCTION IF EXISTS calculate_hot_score(TIMESTAMPTZ, BIGINT, BIGINT);

CREATE OR REPLACE FUNCTION calculate_hot_score(
    p_created_at TIMESTAMPTZ,
    p_vote_score BIGINT,
    p_verification_count BIGINT
)
RETURNS DOUBLE PRECISION
LANGUAGE plpgsql IMMUTABLE
AS $$
DECLARE
    v_hours_ago DOUBLE PRECISION;
    v_time_decay DOUBLE PRECISION;
    v_freshness_boost DOUBLE PRECISION;
BEGIN
    v_hours_ago := EXTRACT(EPOCH FROM (now() - p_created_at)) / 3600.0;

    -- Time decay: content loses 50% of its score every 48 hours
    v_time_decay := POWER(0.5, v_hours_ago / 48.0);

    -- Freshness boost: 2x for content less than 6 hours old
    IF v_hours_ago < 6 THEN
        v_freshness_boost := 2.0;
    ELSE
        v_freshness_boost := 1.0;
    END IF;

    -- hot_score = (vote_score × time_decay × freshness_boost) + (verification_count × 0.5)
    RETURN (COALESCE(p_vote_score, 0) * v_time_decay * v_freshness_boost)
         + (COALESCE(p_verification_count, 0) * 0.5);
END;
$$;

-- =============================================================================
-- 2. Update v_community_photos view with vote counts + report count
-- =============================================================================
DROP VIEW IF EXISTS v_community_photos CASCADE;

CREATE OR REPLACE VIEW v_community_photos AS
SELECT
    cp.*,
    p.username AS reporter_username,
    p.avatar_url AS reporter_avatar,
    COALESCE(v.net_score, 0) AS vote_score,
    COALESCE(v.upvotes, 0) AS upvote_count,
    COALESCE(v.downvotes, 0) AS downvote_count,
    COALESCE(r.report_count, 0) AS report_count
FROM community_photos cp
LEFT JOIN profiles p ON p.id = cp.user_id
LEFT JOIN LATERAL (
    SELECT
        SUM(cv.vote_value) AS net_score,
        COUNT(*) FILTER (WHERE cv.vote_value = 1) AS upvotes,
        COUNT(*) FILTER (WHERE cv.vote_value = -1) AS downvotes
    FROM content_votes cv
    WHERE cv.content_type = 'photo'
      AND cv.content_id = cp.id::TEXT
) v ON true
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS report_count
    FROM content_reports cr
    WHERE cr.content_type = 'photo'
      AND cr.content_id = cp.id::TEXT
) r ON true;

-- =============================================================================
-- 3. Update v_unified_potholes view with vote counts + report count
-- =============================================================================
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
        SELECT rm.user_id
        FROM rides_metadata rm
        WHERE rm.id = vp.ride_id
        ORDER BY rm.created_at ASC
        LIMIT 1
    ) AS reporter_user_id,
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
    vp.address_geocoded_at,
    COALESCE(v.net_score, 0) AS vote_score,
    COALESCE(v.upvotes, 0) AS upvote_count,
    COALESCE(v.downvotes, 0) AS downvote_count,
    COALESCE(r.report_count, 0) AS report_count
FROM verified_potholes vp
LEFT JOIN LATERAL (
    SELECT
        SUM(cv.vote_value) AS net_score,
        COUNT(*) FILTER (WHERE cv.vote_value = 1) AS upvotes,
        COUNT(*) FILTER (WHERE cv.vote_value = -1) AS downvotes
    FROM content_votes cv
    WHERE cv.content_type = 'pothole'
      AND cv.content_id = vp.id::TEXT
) v ON true
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS report_count
    FROM content_reports cr
    WHERE cr.content_type = 'pothole'
      AND cr.content_id = vp.id::TEXT
) r ON true;

-- =============================================================================
-- 4. get_feed_photos function
-- =============================================================================
DROP FUNCTION IF EXISTS get_feed_photos(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_photos(
    p_offset INTEGER DEFAULT 0,
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    id BIGINT,
    user_id UUID,
    image_url TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    street TEXT,
    barangay TEXT,
    city TEXT,
    province TEXT,
    region TEXT,
    country TEXT,
    formatted_address TEXT,
    address_geocoded_at TIMESTAMPTZ,
    detection_status TEXT,
    worst_severity TEXT,
    confidence DOUBLE PRECISION,
    class_name TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    reporter_username TEXT,
    reporter_avatar TEXT,
    vote_score BIGINT,
    upvote_count BIGINT,
    downvote_count BIGINT,
    report_count BIGINT,
    hot_score DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        vcp.id,
        vcp.user_id,
        vcp.image_url,
        vcp.latitude,
        vcp.longitude,
        vcp.street,
        vcp.barangay,
        vcp.city,
        vcp.province,
        vcp.region,
        vcp.country,
        vcp.formatted_address,
        vcp.address_geocoded_at,
        vcp.detection_status,
        vcp.worst_severity,
        vcp.confidence,
        vcp.class_name,
        vcp.created_at,
        vcp.updated_at,
        vcp.reporter_username,
        vcp.reporter_avatar,
        vcp.vote_score,
        vcp.upvote_count,
        vcp.downvote_count,
        vcp.report_count,
        calculate_hot_score(
            vcp.created_at,
            vcp.vote_score,
            (SELECT COUNT(*)
             FROM community_photo_comments cpc
             WHERE cpc.photo_id = vcp.id
               AND cpc.body LIKE '✅ Still here%')
        ) AS hot_score
    FROM v_community_photos vcp
    WHERE vcp.detection_status != 'hidden'
    ORDER BY hot_score DESC, vcp.created_at DESC
    OFFSET p_offset
    LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 5. get_feed_potholes function
-- =============================================================================
DROP FUNCTION IF EXISTS get_feed_potholes(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_potholes(
    p_offset INTEGER DEFAULT 0,
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    pothole_id INTEGER,
    consolidated_latitude DOUBLE PRECISION,
    consolidated_longitude DOUBLE PRECISION,
    worst_severity TEXT,
    total_detection_hits INTEGER,
    image_url TEXT,
    caption TEXT,
    latest_activity_at TIMESTAMPTZ,
    detectors_count BIGINT,
    citizen_first_reported_at TIMESTAMPTZ,
    reporter_user_id UUID,
    reporter_username TEXT,
    reporter_avatar TEXT,
    street TEXT,
    barangay TEXT,
    city TEXT,
    province TEXT,
    region TEXT,
    country TEXT,
    formatted_address TEXT,
    address_geocoded_at TIMESTAMPTZ,
    vote_score BIGINT,
    upvote_count BIGINT,
    downvote_count BIGINT,
    report_count BIGINT,
    hot_score DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        vup.pothole_id,
        vup.consolidated_latitude,
        vup.consolidated_longitude,
        vup.worst_severity,
        vup.total_detection_hits,
        vup.image_url,
        vup.caption,
        vup.latest_activity_at,
        vup.detectors_count,
        vup.citizen_first_reported_at,
        vup.reporter_user_id,
        vup.reporter_username,
        vup.reporter_avatar,
        vup.street,
        vup.barangay,
        vup.city,
        vup.province,
        vup.region,
        vup.country,
        vup.formatted_address,
        vup.address_geocoded_at,
        vup.vote_score,
        vup.upvote_count,
        vup.downvote_count,
        vup.report_count,
        calculate_hot_score(
            vup.citizen_first_reported_at,
            vup.vote_score,
            (SELECT COUNT(*)
             FROM detection_comments dc
             WHERE dc.pothole_id = vup.pothole_id
               AND dc.body LIKE '✅ Still here%')
        ) AS hot_score
    FROM v_unified_potholes vup
    WHERE vup.caption NOT LIKE '[HIDDEN]%'
    ORDER BY hot_score DESC, vup.citizen_first_reported_at DESC
    OFFSET p_offset
    LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 6. Grants
-- =============================================================================
GRANT EXECUTE ON FUNCTION calculate_hot_score(TIMESTAMPTZ, BIGINT, BIGINT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;
