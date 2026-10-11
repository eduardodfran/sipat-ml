-- Migration: Owner-editable title + description on own posts (+ silent history log)
-- Applied manually via Supabase SQL Editor (also recorded in _migrations below).
--
-- SUPERSEDES the pending fix_feed_hidden_filtering.sql paste: this file includes
-- its view repairs (activity_status on v_community_photos, hidden_by_admin branches)
-- AND the hot_score_last_activity.sql bodies (last-activity decay). Pasting this one
-- file leaves prod in the correct final state regardless of apply order.
--
-- What this adds:
--   1. title TEXT + description TEXT on community_photos and verified_potholes.
--      These are USER-OWNED. The pipeline worker only regenerates `caption`, so
--      merges can never clobber user titles/descriptions (worker update dict does
--      not include these columns; merges are in-place row updates, no rows dropped).
--   2. title_history audit log: the FastAPI edit endpoints insert the previous
--      values here on every edit (reason='edit'). No UI reads it (v1); admins can
--      inspect it if an edit is ever disputed. Written via service-role only.
--   3. v_unified_potholes (explicit list) gains vp.title, vp.description;
--      v_community_photos gains them via cp.* (recreated so the frozen column list
--      picks them up -- same reason as the activity_status repair).
--   4. get_feed_photos / get_feed_potholes gain title/description outputs.
--      Hidden-content filters ([HIDDEN] caption prefix / detection_status='hidden')
--      are unchanged.

-- =============================================================================
-- 1. New user-owned columns
-- =============================================================================
ALTER TABLE community_photos
  ADD COLUMN IF NOT EXISTS title TEXT,
  ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE verified_potholes
  ADD COLUMN IF NOT EXISTS title TEXT,
  ADD COLUMN IF NOT EXISTS description TEXT;

-- =============================================================================
-- 2. Silent edit-history log (no RLS policies: service-role writes only)
-- =============================================================================
CREATE TABLE IF NOT EXISTS title_history (
  id BIGSERIAL PRIMARY KEY,
  post_type TEXT NOT NULL CHECK (post_type IN ('pothole', 'photo')),
  post_id BIGINT NOT NULL,
  user_id UUID,
  old_title TEXT,
  old_description TEXT,
  new_title TEXT,
  new_description TEXT,
  reason TEXT NOT NULL DEFAULT 'edit',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS title_history_post_idx
  ON title_history (post_type, post_id);

-- =============================================================================
-- 3. calculate_hot_score (idempotent; last-activity version)
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
-- 4. v_unified_potholes (+ title, description; keeps admin + activity branches)
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
    vp.title,
    vp.description,
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
-- 5. v_community_photos (recreate so cp.* picks up title/description)
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
-- 6. get_feed_potholes (+ title, description; last-activity decay; hidden filter)
-- =============================================================================
DROP FUNCTION IF EXISTS get_feed_potholes(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_potholes(p_offset INTEGER DEFAULT 0, p_limit INTEGER DEFAULT 20)
RETURNS TABLE (
    pothole_id BIGINT, consolidated_latitude DOUBLE PRECISION, consolidated_longitude DOUBLE PRECISION,
    worst_severity TEXT, total_detection_hits INTEGER, image_url TEXT, caption TEXT,
    title TEXT, description TEXT,
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
        vup.title,
        vup.description,
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
-- 7. get_feed_photos (+ title, description; last-activity decay; hidden filter)
-- =============================================================================
DROP FUNCTION IF EXISTS get_feed_photos(INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION get_feed_photos(p_offset INTEGER DEFAULT 0, p_limit INTEGER DEFAULT 20)
RETURNS TABLE (
    id BIGINT, user_id UUID, image_url TEXT, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
    street TEXT, barangay TEXT, city TEXT, province TEXT, region TEXT, country TEXT,
    formatted_address TEXT, address_geocoded_at TIMESTAMPTZ, detection_status TEXT,
    worst_severity TEXT, confidence DOUBLE PRECISION, class_name TEXT,
    caption TEXT, title TEXT, description TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
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
        vcp.caption, vcp.title, vcp.description, vcp.created_at, vcp.updated_at,
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
-- 8. Grants + migration record
-- =============================================================================
GRANT EXECUTE ON FUNCTION calculate_hot_score(TIMESTAMPTZ, BIGINT, BIGINT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;

DO $$
BEGIN
    INSERT INTO _migrations (filename, applied_at)
    VALUES ('add_title_description_to_posts.sql', now())
    ON CONFLICT (filename) DO NOTHING;
EXCEPTION
    WHEN undefined_table OR undefined_object THEN
        NULL;
END $$;
