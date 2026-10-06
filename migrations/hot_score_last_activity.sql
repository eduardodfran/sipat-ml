-- Migration: hot score decays from LAST ACTIVITY instead of first report
-- Applied manually via Supabase SQL Editor (also recorded in _migrations below).
--
-- Problem: calculate_hot_score was fed citizen_first_reported_at (potholes) /
-- created_at (photos), so every item older than ~48h decays to ~0 and "hot"
-- only ever ranks old content by verification count. Content that receives a
-- fresh vote or comment could never resurface.
--
-- Fix: feed the function a last-activity timestamp =
--   GREATEST(row updated_at, latest comment, latest vote)
-- and make the function null-safe + STABLE (it reads now(), so IMMUTABLE was wrong).

-- =============================================================================
-- 1. calculate_hot_score: STABLE, null-safe age
-- =============================================================================
DROP FUNCTION IF EXISTS calculate_hot_score(TIMESTAMPTZ, BIGINT, BIGINT);

CREATE OR REPLACE FUNCTION calculate_hot_score(
    p_created_at TIMESTAMPTZ,
    p_vote_score BIGINT,
    p_verification_count BIGINT
)
RETURNS DOUBLE PRECISION
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_hours_ago DOUBLE PRECISION;
    v_time_decay DOUBLE PRECISION;
    v_freshness_boost DOUBLE PRECISION;
BEGIN
    -- Unknown age scores as 30 days old (decay ~0) instead of brand new.
    v_hours_ago := COALESCE(EXTRACT(EPOCH FROM (now() - p_created_at)) / 3600.0, 720.0);

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
-- 2. get_feed_potholes: same signature, decay from last activity
-- =============================================================================
DROP FUNCTION IF EXISTS get_feed_potholes(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_potholes(p_offset INTEGER DEFAULT 0, p_limit INTEGER DEFAULT 20)
RETURNS TABLE (
    pothole_id BIGINT, consolidated_latitude DOUBLE PRECISION, consolidated_longitude DOUBLE PRECISION,
    worst_severity TEXT, total_detection_hits INTEGER, image_url TEXT, caption TEXT,
    latest_activity_at TIMESTAMPTZ, detectors_count BIGINT, citizen_first_reported_at TIMESTAMPTZ,
    reporter_user_id UUID, reporter_username TEXT, reporter_avatar TEXT,
    street TEXT, barangay TEXT, city TEXT, province TEXT, region TEXT, country TEXT,
    formatted_address TEXT, address_geocoded_at TIMESTAMPTZ,
    vote_score BIGINT, upvote_count BIGINT, downvote_count BIGINT, report_count BIGINT,
    visibility_status TEXT, hot_score DOUBLE PRECISION, user_vote SMALLINT,
    activity_status TEXT, fixed_count INTEGER, still_count INTEGER
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        vup.pothole_id,
        vup.consolidated_latitude::DOUBLE PRECISION,
        vup.consolidated_longitude::DOUBLE PRECISION,
        vup.worst_severity,
        vup.total_detection_hits::INTEGER,
        vup.image_url,
        vup.caption,
        vup.latest_activity_at,
        vup.detectors_count::BIGINT,
        vup.citizen_first_reported_at,
        vup.reporter_user_id,
        vup.reporter_username,
        vup.reporter_avatar,
        vup.street, vup.barangay, vup.city, vup.province, vup.region, vup.country,
        vup.formatted_address, vup.address_geocoded_at,
        vup.vote_score::BIGINT,
        vup.upvote_count::BIGINT,
        vup.downvote_count::BIGINT,
        vup.report_count::BIGINT,
        vup.visibility_status,
        calculate_hot_score(
            GREATEST(
                vup.latest_activity_at,
                (SELECT MAX(dc.created_at) FROM detection_comments dc
                  WHERE dc.pothole_id = vup.pothole_id),
                (SELECT MAX(cv.created_at) FROM content_votes cv
                  WHERE cv.content_type = 'pothole' AND cv.content_id = vup.pothole_id::TEXT)
            ),
            vup.vote_score,
            (SELECT COUNT(*) FROM detection_comments dc
              WHERE dc.pothole_id = vup.pothole_id AND dc.body LIKE '✅ Still here%')
        )::DOUBLE PRECISION AS hot_score,
        COALESCE((SELECT cv.vote_value FROM content_votes cv
                  WHERE cv.content_type = 'pothole' AND cv.content_id = vup.pothole_id::TEXT AND cv.user_id = auth.uid()), 0)::SMALLINT AS user_vote,
        vup.activity_status, vup.fixed_count::INTEGER, vup.still_count::INTEGER
    FROM v_unified_potholes vup
    WHERE vup.visibility_status = 'visible'
      AND vup.activity_status = 'active'
      AND vup.caption NOT LIKE '[HIDDEN]%'
    ORDER BY hot_score DESC, vup.latest_activity_at DESC NULLS LAST
    OFFSET p_offset LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 3. get_feed_photos: same signature, decay from last activity
-- =============================================================================
DROP FUNCTION IF EXISTS get_feed_photos(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_photos(p_offset INTEGER DEFAULT 0, p_limit INTEGER DEFAULT 20)
RETURNS TABLE (
    id BIGINT, user_id UUID, image_url TEXT, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
    street TEXT, barangay TEXT, city TEXT, province TEXT, region TEXT, country TEXT,
    formatted_address TEXT, address_geocoded_at TIMESTAMPTZ, detection_status TEXT,
    worst_severity TEXT, confidence DOUBLE PRECISION, class_name TEXT,
    caption TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
    reporter_username TEXT, reporter_avatar TEXT, vote_score BIGINT,
    upvote_count BIGINT, downvote_count BIGINT, report_count BIGINT,
    visibility_status TEXT, hot_score DOUBLE PRECISION, user_vote SMALLINT,
    activity_status TEXT, fixed_count INTEGER, still_count INTEGER
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        vcp.id, vcp.user_id, vcp.image_url, vcp.latitude, vcp.longitude,
        vcp.street, vcp.barangay, vcp.city, vcp.province, vcp.region, vcp.country,
        vcp.formatted_address, vcp.address_geocoded_at, vcp.detection_status,
        vcp.worst_severity, vcp.confidence, vcp.class_name,
        vcp.caption, vcp.created_at, vcp.updated_at,
        vcp.reporter_username, vcp.reporter_avatar, vcp.vote_score,
        vcp.upvote_count::BIGINT, vcp.downvote_count::BIGINT, vcp.report_count::BIGINT,
        vcp.visibility_status,
        calculate_hot_score(
            GREATEST(
                vcp.updated_at,
                (SELECT MAX(cpc.created_at) FROM community_photo_comments cpc
                  WHERE cpc.photo_id = vcp.id),
                (SELECT MAX(cv.created_at) FROM content_votes cv
                  WHERE cv.content_type = 'photo' AND cv.content_id = vcp.id::TEXT)
            ),
            vcp.vote_score,
            (SELECT COUNT(*) FROM community_photo_comments cpc
              WHERE cpc.photo_id = vcp.id AND cpc.body LIKE '✅ Still here%')
        ) AS hot_score,
        COALESCE((SELECT cv.vote_value FROM content_votes cv
                  WHERE cv.content_type = 'photo' AND cv.content_id = vcp.id::TEXT AND cv.user_id = auth.uid()), 0)::SMALLINT AS user_vote,
        vcp.activity_status, vcp.fixed_count, vcp.still_count
    FROM v_community_photos vcp
    WHERE vcp.visibility_status = 'visible'
      AND vcp.activity_status = 'active'
      AND vcp.detection_status != 'hidden'
    ORDER BY hot_score DESC, vcp.updated_at DESC NULLS LAST
    OFFSET p_offset LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 4. Grants + migration record
-- =============================================================================
GRANT EXECUTE ON FUNCTION calculate_hot_score(TIMESTAMPTZ, BIGINT, BIGINT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;

DO $$
BEGIN
    INSERT INTO _migrations (filename, applied_at)
    VALUES ('hot_score_last_activity.sql', now())
    ON CONFLICT (filename) DO NOTHING;
EXCEPTION
    WHEN undefined_table OR undefined_object THEN
        NULL;
END $$;
