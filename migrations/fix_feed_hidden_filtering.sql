-- Migration: Fix hidden-content filtering in feed + repair stale v_community_photos
-- Applied manually via Supabase SQL Editor (also recorded in _migrations below).
--
-- Problems fixed (verified against live DB):
--   1. get_feed_potholes filtered visibility/activity but NOT the '[HIDDEN] ' caption
--      prefix, so admin-hidden potholes appeared in the mobile feed (41/100 rows leaked).
--   2. v_community_photos was created before activity_status/fixed_count/still_count
--      existed on community_photos, so the view lacks those columns -> get_feed_photos
--      hard-errors ("column vcp.activity_status does not exist") and every direct
--      .eq('activity_status', ...) query on the view fails (dashboard photo section,
--      map community photos).
--   3. Direct view consumers had no hidden filters -> added hidden_by_admin branches
--      to the views' visibility_status CASE so visibility_status = 'visible' is now
--      the single authoritative "show it" flag.

-- =============================================================================
-- 1. v_unified_potholes: deployed definition (add_fixed_verification) + admin branch
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
    vp.activity_status,
    vp.fixed_count,
    vp.still_count,
    COALESCE(v.net_score, 0) AS vote_score,
    COALESCE(v.upvotes, 0) AS upvote_count,
    COALESCE(v.downvotes, 0) AS downvote_count,
    COALESCE(r.report_count, 0) AS report_count,
    CASE
        WHEN vp.caption LIKE '[HIDDEN]%' THEN 'hidden_by_admin'
        WHEN COALESCE(r.report_count, 0) >= 3 THEN 'hidden_by_reports'
        WHEN COALESCE(v.downvotes, 0) >= 3
             AND (COALESCE(v.downvotes, 0)::FLOAT / NULLIF(COALESCE(v.upvotes, 0) + COALESCE(v.downvotes, 0), 0)) >= 0.7
             THEN 'hidden_by_votes'
        ELSE 'visible'
    END AS visibility_status
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
-- 2. v_community_photos: recreate so cp.* picks up activity_status/fixed_count/
--    still_count (table columns added AFTER the old view was frozen), + admin branch
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
    COALESCE(r.report_count, 0) AS report_count,
    CASE
        WHEN cp.detection_status = 'hidden' THEN 'hidden_by_admin'
        WHEN COALESCE(r.report_count, 0) >= 3 THEN 'hidden_by_reports'
        WHEN COALESCE(v.downvotes, 0) >= 3
             AND (COALESCE(v.downvotes, 0)::FLOAT / NULLIF(COALESCE(v.upvotes, 0) + COALESCE(v.downvotes, 0), 0)) >= 0.7
             THEN 'hidden_by_votes'
        ELSE 'visible'
    END AS visibility_status
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
-- 3. get_feed_potholes: deployed signature + '[HIDDEN] ' caption filter
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
        calculate_hot_score(vup.citizen_first_reported_at, vup.vote_score,
            (SELECT COUNT(*) FROM detection_comments dc
             WHERE dc.pothole_id = vup.pothole_id AND dc.body LIKE '✅ Still here%'))::DOUBLE PRECISION AS hot_score,
        COALESCE((SELECT cv.vote_value FROM content_votes cv
                  WHERE cv.content_type = 'pothole' AND cv.content_id = vup.pothole_id::TEXT AND cv.user_id = auth.uid()), 0)::SMALLINT AS user_vote,
        vup.activity_status, vup.fixed_count::INTEGER, vup.still_count::INTEGER
    FROM v_unified_potholes vup
    WHERE vup.visibility_status = 'visible'
      AND vup.activity_status = 'active'
      AND vup.caption NOT LIKE '[HIDDEN]%'
    ORDER BY hot_score DESC, vup.citizen_first_reported_at DESC
    OFFSET p_offset LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 4. get_feed_photos: deployed signature, body repaired (view now has the columns)
--    + hide photos marked detection_status = 'hidden'
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
        calculate_hot_score(vcp.created_at, vcp.vote_score,
            (SELECT COUNT(*) FROM community_photo_comments cpc
             WHERE cpc.photo_id = vcp.id AND cpc.body LIKE '✅ Still here%')) AS hot_score,
        COALESCE((SELECT cv.vote_value FROM content_votes cv
                  WHERE cv.content_type = 'photo' AND cv.content_id = vcp.id::TEXT AND cv.user_id = auth.uid()), 0)::SMALLINT AS user_vote,
        vcp.activity_status, vcp.fixed_count, vcp.still_count
    FROM v_community_photos vcp
    WHERE vcp.visibility_status = 'visible'
      AND vcp.activity_status = 'active'
      AND vcp.detection_status != 'hidden'
    ORDER BY hot_score DESC, vcp.created_at DESC
    OFFSET p_offset LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 5. Grants + migration record
-- =============================================================================
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;

DO $$
BEGIN
    INSERT INTO _migrations (filename, applied_at)
    VALUES ('fix_feed_hidden_filtering.sql', now())
    ON CONFLICT (filename) DO NOTHING;
EXCEPTION
    WHEN undefined_table OR undefined_object THEN
        NULL;
END $$;
