-- Fix: Add user_vote to feed functions so votes persist across navigation
-- Run this in Supabase SQL Editor

-- 1. Fix get_feed_photos
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
    confidence NUMERIC,
    class_name TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    reporter_username TEXT,
    reporter_avatar TEXT,
    vote_score BIGINT,
    upvote_count BIGINT,
    downvote_count BIGINT,
    report_count BIGINT,
    hot_score DOUBLE PRECISION,
    user_vote SMALLINT
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
        ) AS hot_score,
        COALESCE(
            (SELECT cv.vote_value FROM content_votes cv
             WHERE cv.content_type = 'photo'
               AND cv.content_id = vcp.id::TEXT
               AND cv.user_id = auth.uid()),
            0
        )::SMALLINT AS user_vote
    FROM v_community_photos vcp
    WHERE vcp.detection_status != 'hidden'
    ORDER BY hot_score DESC, vcp.created_at DESC
    OFFSET p_offset
    LIMIT p_limit;
END;
$$;

-- 2. Fix get_feed_potholes
DROP FUNCTION IF EXISTS get_feed_potholes(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_potholes(
    p_offset INTEGER DEFAULT 0,
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    pothole_id BIGINT,
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
    hot_score DOUBLE PRECISION,
    user_vote SMALLINT
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
        ) AS hot_score,
        COALESCE(
            (SELECT cv.vote_value FROM content_votes cv
             WHERE cv.content_type = 'pothole'
               AND cv.content_id = vup.pothole_id::TEXT
               AND cv.user_id = auth.uid()),
            0
        )::SMALLINT AS user_vote
    FROM v_unified_potholes vup
    WHERE vup.caption NOT LIKE '[HIDDEN]%'
    ORDER BY hot_score DESC, vup.citizen_first_reported_at DESC
    OFFSET p_offset
    LIMIT p_limit;
END;
$$;

-- 3. Grant permissions
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;
