-- Fix feed + votes: Run this entire script in Supabase SQL Editor.
-- Safe to run multiple times (idempotent).

-- =============================================================================
-- 1. Ensure tables exist
-- =============================================================================

CREATE TABLE IF NOT EXISTS community_photos (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  image_url TEXT NOT NULL,
  latitude DOUBLE PRECISION NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  street TEXT, barangay TEXT, city TEXT, province TEXT, region TEXT, country TEXT,
  formatted_address TEXT, address_geocoded_at TIMESTAMPTZ,
  detection_status TEXT NOT NULL DEFAULT 'pending',
  worst_severity TEXT, confidence DOUBLE PRECISION, class_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE community_photos ADD COLUMN IF NOT EXISTS caption TEXT;

CREATE TABLE IF NOT EXISTS content_votes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  content_type TEXT NOT NULL CHECK (content_type IN ('photo', 'pothole')),
  content_id TEXT NOT NULL,
  vote_value SMALLINT NOT NULL CHECK (vote_value IN (-1, 1)),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, content_type, content_id)
);
CREATE INDEX IF NOT EXISTS idx_content_votes_content ON content_votes(content_type, content_id);
CREATE INDEX IF NOT EXISTS idx_content_votes_user_id ON content_votes(user_id);
ALTER TABLE content_votes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Anyone can view votes" ON content_votes;
CREATE POLICY "Anyone can view votes" ON content_votes FOR SELECT USING (true);
DROP POLICY IF EXISTS "Authenticated users can insert votes" ON content_votes;
CREATE POLICY "Authenticated users can insert votes" ON content_votes FOR INSERT WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Authenticated users can update own votes" ON content_votes;
CREATE POLICY "Authenticated users can update own votes" ON content_votes FOR UPDATE USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "Authenticated users can delete own votes" ON content_votes;
CREATE POLICY "Authenticated users can delete own votes" ON content_votes FOR DELETE USING (auth.uid() = user_id);

-- =============================================================================
-- 2. Recreate v_community_photos view with vote counts + report count
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
-- 3. calculate_hot_score function
-- =============================================================================

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
    v_time_decay := POWER(0.5, v_hours_ago / 48.0);
    IF v_hours_ago < 6 THEN
        v_freshness_boost := 2.0;
    ELSE
        v_freshness_boost := 1.0;
    END IF;
    RETURN (COALESCE(p_vote_score, 0) * v_time_decay * v_freshness_boost)
         + (COALESCE(p_verification_count, 0) * 0.5);
END;
$$;

-- =============================================================================
-- 4. get_feed_photos — CORRECTED: id BIGINT (not UUID), with user_vote
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
    caption TEXT,
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
        vcp.caption,
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

-- =============================================================================
-- 5. get_feed_potholes — CORRECTED: pothole_id BIGINT (not INTEGER), with user_vote
-- =============================================================================

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
        vup.street,
        vup.barangay,
        vup.city,
        vup.province,
        vup.region,
        vup.country,
        vup.formatted_address,
        vup.address_geocoded_at,
        vup.vote_score::BIGINT,
        vup.upvote_count::BIGINT,
        vup.downvote_count::BIGINT,
        vup.report_count::BIGINT,
        calculate_hot_score(
            vup.citizen_first_reported_at,
            vup.vote_score,
            (SELECT COUNT(*)
             FROM detection_comments dc
             WHERE dc.pothole_id = vup.pothole_id
               AND dc.body LIKE '✅ Still here%')
        )::DOUBLE PRECISION AS hot_score,
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

-- =============================================================================
-- 6. Vote RPCs
-- =============================================================================

DROP FUNCTION IF EXISTS get_content_votes(TEXT, TEXT);
CREATE OR REPLACE FUNCTION get_content_votes(p_content_type TEXT, p_content_id TEXT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY
  SELECT
    COUNT(*) FILTER (WHERE cv.vote_value = 1) AS upvotes,
    COUNT(*) FILTER (WHERE cv.vote_value = -1) AS downvotes,
    COALESCE(SUM(cv.vote_value), 0) AS net_score,
    COALESCE(
      (SELECT cv2.vote_value FROM content_votes cv2
       WHERE cv2.content_type = p_content_type
         AND cv2.content_id = p_content_id
         AND cv2.user_id = auth.uid()),
      0
    )::SMALLINT AS user_vote
  FROM content_votes cv
  WHERE cv.content_type = p_content_type
    AND cv.content_id = p_content_id;
END;
$$;

DROP FUNCTION IF EXISTS vote_content(TEXT, TEXT, SMALLINT);
CREATE OR REPLACE FUNCTION vote_content(p_content_type TEXT, p_content_id TEXT, p_vote_value SMALLINT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated';
  END IF;
  INSERT INTO content_votes (user_id, content_type, content_id, vote_value)
  VALUES (v_user_id, p_content_type, p_content_id, p_vote_value)
  ON CONFLICT (user_id, content_type, content_id)
  DO UPDATE SET vote_value = p_vote_value, created_at = now();
  RETURN QUERY SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

DROP FUNCTION IF EXISTS unvote_content(TEXT, TEXT);
CREATE OR REPLACE FUNCTION unvote_content(p_content_type TEXT, p_content_id TEXT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated';
  END IF;
  DELETE FROM content_votes
  WHERE user_id = v_user_id
    AND content_type = p_content_type
    AND content_id = p_content_id;
  RETURN QUERY SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

-- =============================================================================
-- 7. Grants
-- =============================================================================

GRANT EXECUTE ON FUNCTION calculate_hot_score(TIMESTAMPTZ, BIGINT, BIGINT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_content_votes(TEXT, TEXT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION vote_content(TEXT, TEXT, SMALLINT) TO authenticated;
GRANT EXECUTE ON FUNCTION unvote_content(TEXT, TEXT) TO authenticated;
