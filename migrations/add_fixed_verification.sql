-- Migration: Community consensus for Still Here / Fixed (no admin)
-- Simplified for defense: single threshold, fixed excluded by default (no toggle yet)
-- Rule: FIXED when fixed_count >= 3 AND fixed_count > still_count, else ACTIVE
-- One signal per user per hazard (last-wins via upsert). Login required (auth.uid()).

-- 1. Columns on verified_potholes ------------------------------------------------
ALTER TABLE verified_potholes
  ADD COLUMN IF NOT EXISTS activity_status TEXT NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS fixed_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS still_count INTEGER NOT NULL DEFAULT 0;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'verified_potholes_activity_status_check') THEN
    ALTER TABLE verified_potholes
      ADD CONSTRAINT verified_potholes_activity_status_check
      CHECK (activity_status IN ('active', 'fixed'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_verified_potholes_activity_status
  ON verified_potholes(activity_status);
CREATE INDEX IF NOT EXISTS idx_verified_potholes_activity_lat_lng
  ON verified_potholes(activity_status, consolidated_latitude, consolidated_longitude);

-- 2. Columns on community_photos --------------------------------------------------
ALTER TABLE community_photos
  ADD COLUMN IF NOT EXISTS activity_status TEXT NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS fixed_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS still_count INTEGER NOT NULL DEFAULT 0;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'community_photos_activity_status_check') THEN
    ALTER TABLE community_photos
      ADD CONSTRAINT community_photos_activity_status_check
      CHECK (activity_status IN ('active', 'fixed'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_community_photos_activity_status
  ON community_photos(activity_status);

-- 3. Verifications table (one row per user per hazard, last-wins) -----------------
CREATE TABLE IF NOT EXISTS hazard_verifications (
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  content_type TEXT NOT NULL CHECK (content_type IN ('pothole', 'photo')),
  content_id TEXT NOT NULL,
  signal TEXT NOT NULL CHECK (signal IN ('still', 'fixed')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, content_type, content_id)
);

CREATE INDEX IF NOT EXISTS idx_hazard_verifications_lookup
  ON hazard_verifications(content_type, content_id, signal);

ALTER TABLE hazard_verifications ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Anyone can view verifications" ON hazard_verifications;
CREATE POLICY "Anyone can view verifications" ON hazard_verifications FOR SELECT USING (true);
DROP POLICY IF EXISTS "Users manage own verifications" ON hazard_verifications;
CREATE POLICY "Users manage own verifications" ON hazard_verifications FOR ALL
  USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- 4. RPC: mark signal (upsert + recompute + set status) ---------------------------
DROP FUNCTION IF EXISTS mark_hazard_signal(TEXT, TEXT, TEXT);
CREATE OR REPLACE FUNCTION mark_hazard_signal(
  p_content_type TEXT, p_content_id TEXT, p_signal TEXT
)
RETURNS TABLE (fixed_count INTEGER, still_count INTEGER, activity_status TEXT)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
  v_fixed INTEGER := 0;
  v_still INTEGER := 0;
  v_status TEXT := 'active';
BEGIN
  IF v_user_id IS NULL THEN RAISE EXCEPTION 'Not authenticated'; END IF;
  IF p_content_type NOT IN ('pothole', 'photo') THEN RAISE EXCEPTION 'Bad content_type'; END IF;
  IF p_signal NOT IN ('still', 'fixed') THEN RAISE EXCEPTION 'Bad signal'; END IF;

  INSERT INTO hazard_verifications (user_id, content_type, content_id, signal)
  VALUES (v_user_id, p_content_type, p_content_id, p_signal)
  ON CONFLICT (user_id, content_type, content_id)
  DO UPDATE SET signal = EXCLUDED.signal, updated_at = now();

  SELECT COUNT(*) FILTER (WHERE signal = 'fixed'), COUNT(*) FILTER (WHERE signal = 'still')
  INTO v_fixed, v_still
  FROM hazard_verifications
  WHERE content_type = p_content_type AND content_id = p_content_id;

  IF v_fixed >= 3 AND v_fixed > v_still THEN v_status := 'fixed'; ELSE v_status := 'active'; END IF;

  IF p_content_type = 'pothole' THEN
    UPDATE verified_potholes
    SET fixed_count = v_fixed, still_count = v_still, activity_status = v_status,
        updated_at = now()
    WHERE id::TEXT = p_content_id;
  ELSE
    UPDATE community_photos
    SET fixed_count = v_fixed, still_count = v_still, activity_status = v_status,
        updated_at = now()
    WHERE id::TEXT = p_content_id;
  END IF;

  fixed_count := v_fixed; still_count := v_still; activity_status := v_status;
  RETURN NEXT;
END;
$$;

-- 5. RPC: get counts + my signal + status ----------------------------------------
DROP FUNCTION IF EXISTS get_hazard_verification(TEXT, TEXT);
CREATE OR REPLACE FUNCTION get_hazard_verification(p_content_type TEXT, p_content_id TEXT)
RETURNS TABLE (fixed_count INTEGER, still_count INTEGER, activity_status TEXT, my_signal TEXT)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY
  SELECT
    COALESCE((SELECT COUNT(*) FILTER (WHERE signal='fixed') FROM hazard_verifications hv
      WHERE hv.content_type=p_content_type AND hv.content_id=p_content_id),0)::INTEGER,
    COALESCE((SELECT COUNT(*) FILTER (WHERE signal='still') FROM hazard_verifications hv
      WHERE hv.content_type=p_content_type AND hv.content_id=p_content_id),0)::INTEGER,
    COALESCE((CASE WHEN p_content_type='pothole'
      THEN (SELECT vp.activity_status FROM verified_potholes vp WHERE vp.id::TEXT=p_content_id)
      ELSE (SELECT cp.activity_status FROM community_photos cp WHERE cp.id::TEXT=p_content_id) END), 'active'),
    (SELECT hv.signal FROM hazard_verifications hv
      WHERE hv.content_type=p_content_type AND hv.content_id=p_content_id AND hv.user_id=auth.uid()
      LIMIT 1);
END;
$$;

GRANT EXECUTE ON FUNCTION mark_hazard_signal(TEXT, TEXT, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION get_hazard_verification(TEXT, TEXT) TO anon, authenticated;

-- 6. Views: expose activity_status + counts (filtered by callers, default active) --
-- v_community_photos uses cp.* so new columns flow automatically; ensure view exists.
-- v_unified_potholes is explicit: recreate with new columns appended.

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
    vp.activity_status,
    vp.fixed_count,
    vp.still_count,
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

-- 7. Feed functions: exclude fixed by default (no toggle yet = simplified defense) -
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
    ORDER BY hot_score DESC, vup.citizen_first_reported_at DESC
    OFFSET p_offset LIMIT p_limit;
END;
$$;

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
        vcp.upvote_count, vcp.downvote_count, vcp.report_count,
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
    ORDER BY hot_score DESC, vcp.created_at DESC
    OFFSET p_offset LIMIT p_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION get_feed_photos(INTEGER, INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION get_feed_potholes(INTEGER, INTEGER) TO anon, authenticated;
