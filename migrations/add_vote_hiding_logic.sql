-- Migration: Add vote-based visibility filtering
-- Hide content when downvote_count >= 3 AND downvote_ratio >= 0.7 (70%)

-- =============================================================================
-- 1. Update v_community_photos to include visibility_status
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
-- 2. Update v_unified_potholes to include visibility_status
-- =============================================================================
DROP VIEW IF EXISTS v_unified_potholes CASCADE;

CREATE OR REPLACE VIEW v_unified_potholes AS
SELECT
    vp.id AS pothole_id,
    vp.consolidated_latitude,
    vp.consolidated_longitude,
    vp.worst_severity,
    vp.total_detection_hits,
    COALESCE(vp.image_url,
        (SELECT rd.image_url
         FROM raw_detections rd
         JOIN rides_metadata rm ON rm.id = rd.ride_id
         WHERE rd.image_url IS NOT NULL
           AND _hap_distance(vp.consolidated_latitude, vp.consolidated_longitude, rd.lat, rd.lng) <= 15.0
         ORDER BY (rm.created_at + (rd.video_timestamp || ' seconds')::INTERVAL) DESC
         LIMIT 1)
    ) AS image_url,
    vp.caption,
    vp.updated_at AS latest_activity_at,
    COALESCE(jsonb_array_length(vp.user_detections), 0) AS detectors_count,
    (SELECT rm.created_at FROM rides_metadata rm WHERE rm.id = vp.ride_id ORDER BY rm.created_at ASC LIMIT 1) AS citizen_first_reported_at,
    (SELECT rm.user_id FROM rides_metadata rm WHERE rm.id = vp.ride_id ORDER BY rm.created_at ASC LIMIT 1) AS reporter_user_id,
    (SELECT p.username FROM rides_metadata rm JOIN profiles p ON p.id = rm.user_id WHERE rm.id = vp.ride_id ORDER BY rm.created_at ASC LIMIT 1) AS reporter_username,
    (SELECT p.avatar_url FROM rides_metadata rm JOIN profiles p ON p.id = rm.user_id WHERE rm.id = vp.ride_id ORDER BY rm.created_at ASC LIMIT 1) AS reporter_avatar,
    vp.street, vp.barangay, vp.city, vp.province, vp.region, vp.country,
    vp.formatted_address, vp.address_geocoded_at,
    COALESCE(v.net_score, 0) AS vote_score,
    COALESCE(v.upvotes, 0) AS upvote_count,
    COALESCE(v.downvotes, 0) AS downvote_count,
    COALESCE(r.report_count, 0) AS report_count,
    CASE
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
    WHERE cv.content_type = 'pothole' AND cv.content_id = vp.id::TEXT
) v ON true
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS report_count
    FROM content_reports cr
    WHERE cr.content_type = 'pothole' AND cr.content_id = vp.id::TEXT
) r ON true;

-- =============================================================================
-- 3. Update get_feed_photos to filter by visibility_status
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
    visibility_status TEXT, hot_score DOUBLE PRECISION, user_vote SMALLINT
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
        vcp.upvote_count, vcp.downvote_count, vcp.report_count,
        vcp.visibility_status,
        calculate_hot_score(vcp.created_at, vcp.vote_score,
            (SELECT COUNT(*) FROM community_photo_comments cpc
             WHERE cpc.photo_id = vcp.id AND cpc.body LIKE '✅ Still here%')) AS hot_score,
        COALESCE((SELECT cv.vote_value FROM content_votes cv
                  WHERE cv.content_type = 'photo' AND cv.content_id = vcp.id::TEXT AND cv.user_id = auth.uid()), 0)::SMALLINT AS user_vote
    FROM v_community_photos vcp
    WHERE vcp.visibility_status = 'visible'
    ORDER BY hot_score DESC, vcp.created_at DESC
    OFFSET p_offset LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 4. Update get_feed_potholes to filter by visibility_status
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
    visibility_status TEXT, hot_score DOUBLE PRECISION, user_vote SMALLINT
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
                  WHERE cv.content_type = 'pothole' AND cv.content_id = vup.pothole_id::TEXT AND cv.user_id = auth.uid()), 0)::SMALLINT AS user_vote
    FROM v_unified_potholes vup
    WHERE vup.visibility_status = 'visible'
    ORDER BY hot_score DESC, vup.citizen_first_reported_at DESC
    OFFSET p_offset LIMIT p_limit;
END;
$$;

-- =============================================================================
-- 5. Update report_content to also check vote-based hiding threshold
-- =============================================================================
DROP FUNCTION IF EXISTS report_content(TEXT, TEXT, TEXT);
CREATE OR REPLACE FUNCTION report_content(p_content_type TEXT, p_content_id TEXT, p_reason TEXT)
RETURNS TABLE (report_count BIGINT, is_hidden BOOLEAN, hide_reason TEXT)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
  v_report_count BIGINT;
  v_downvote_count BIGINT;
  v_upvote_count BIGINT;
  v_downvote_ratio FLOAT;
  v_hidden BOOLEAN;
  v_hide_reason TEXT;
BEGIN
  IF v_user_id IS NULL THEN RAISE EXCEPTION 'Not authenticated'; END IF;

  INSERT INTO content_reports (user_id, content_type, content_id, reason)
  VALUES (v_user_id, p_content_type, p_content_id, p_reason)
  ON CONFLICT (user_id, content_type, content_id)
  DO UPDATE SET reason = EXCLUDED.reason, created_at = now();

  SELECT count(*) INTO v_report_count
  FROM content_reports WHERE content_type = p_content_type AND content_id = p_content_id;

  SELECT
    COUNT(*) FILTER (WHERE vote_value = -1),
    COUNT(*) FILTER (WHERE vote_value = 1)
  INTO v_downvote_count, v_upvote_count
  FROM content_votes
  WHERE content_type = p_content_type AND content_id = p_content_id;

  v_downvote_ratio := CASE WHEN (v_downvote_count + v_upvote_count) > 0
                          THEN v_downvote_count::FLOAT / (v_downvote_count + v_upvote_count)
                          ELSE 0.0 END;

  v_hidden := false;
  v_hide_reason := NULL;

  IF v_report_count >= 3 THEN
    v_hidden := true; v_hide_reason := 'reports';
  ELSIF v_downvote_count >= 3 AND v_downvote_ratio >= 0.7 THEN
    v_hidden := true; v_hide_reason := 'votes';
  END IF;

  IF v_hidden THEN
    IF p_content_type = 'photo' THEN
      UPDATE community_photos SET detection_status = 'hidden' WHERE id = p_content_id::BIGINT;
    ELSIF p_content_type = 'pothole' THEN
      UPDATE verified_potholes SET caption = '[HIDDEN] ' || COALESCE(caption, '')
      WHERE id = p_content_id::INTEGER AND (caption IS NULL OR caption NOT LIKE '[HIDDEN]%');
    END IF;
  END IF;

  report_count := v_report_count;
  is_hidden := v_hidden;
  hide_reason := v_hide_reason;
  RETURN NEXT;
END;
$$;

-- =============================================================================
-- 6. Update vote_content/unvote_content to trigger visibility check
-- =============================================================================
DROP FUNCTION IF EXISTS vote_content(TEXT, TEXT, SMALLINT);
CREATE OR REPLACE FUNCTION vote_content(p_content_type TEXT, p_content_id TEXT, p_vote_value SMALLINT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
BEGIN
  IF v_user_id IS NULL THEN RAISE EXCEPTION 'Not authenticated'; END IF;

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
  IF v_user_id IS NULL THEN RAISE EXCEPTION 'Not authenticated'; END IF;

  DELETE FROM content_votes WHERE user_id = v_user_id AND content_type = p_content_type AND content_id = p_content_id;

  RETURN QUERY SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

-- Grants
GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION report_content(TEXT, TEXT, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION unvote_content(TEXT, TEXT) TO authenticated;
