-- Migration: Hidden admin hide/unhide (NOT presented, defense prep only)
-- Single-admin allowlist: franeduardo305@gmail.com
-- Reuses existing hiding mechanism: pothole caption '[HIDDEN] ' prefix,
-- photo detection_status='hidden'. Map/feed already exclude these.

-- Helper: check caller is the admin (SECURITY DEFINER can read auth.users)
CREATE OR REPLACE FUNCTION is_admin()
RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
  RETURN EXISTS (
    SELECT 1 FROM auth.users u
    WHERE u.id = auth.uid()
      AND lower(u.email) = lower('franeduardo305@gmail.com')
  );
END;
$$;

-- List recent content with hidden flags (admin only)
DROP FUNCTION IF EXISTS admin_list_recent(INTEGER);
CREATE OR REPLACE FUNCTION admin_list_recent(p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
  content_type TEXT, content_id TEXT, caption TEXT, street TEXT,
  severity TEXT, is_hidden BOOLEAN, created_at TIMESTAMPTZ, image_url TEXT,
  lat DOUBLE PRECISION, lng DOUBLE PRECISION,
  nearby_image_url TEXT, nearby_m DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
  IF NOT is_admin() THEN RAISE EXCEPTION 'Not authorized'; END IF;

  RETURN QUERY
  WITH recent_potholes AS (
    SELECT 'pothole'::TEXT AS content_type, vp.id::TEXT AS content_id,
           vp.caption AS caption, vp.street AS street,
           vp.worst_severity AS severity,
           (vp.caption LIKE '[HIDDEN]%')::BOOLEAN AS is_hidden,
           vp.updated_at AS created_at,
           COALESCE(vp.image_url,
             (SELECT rd.image_url
              FROM raw_detections rd
              JOIN rides_metadata rm ON rm.id = rd.ride_id
              WHERE rd.image_url IS NOT NULL
                AND _hap_distance(vp.consolidated_latitude, vp.consolidated_longitude, rd.lat, rd.lng) <= 15.0
              ORDER BY (rm.created_at + (rd.video_timestamp || ' seconds')::INTERVAL) DESC
              LIMIT 1)
           ) AS image_url,
           vp.consolidated_latitude AS lat, vp.consolidated_longitude AS lng,
           np.image_url AS nearby_image_url, np.dist AS nearby_m
    FROM verified_potholes vp
    LEFT JOIN LATERAL (
      SELECT cp.image_url AS image_url,
             _hap_distance(vp.consolidated_latitude, vp.consolidated_longitude, cp.latitude, cp.longitude) AS dist
      FROM community_photos cp
      WHERE cp.image_url IS NOT NULL
        AND cp.detection_status != 'hidden'
        AND _hap_distance(vp.consolidated_latitude, vp.consolidated_longitude, cp.latitude, cp.longitude) <= 200.0
      ORDER BY _hap_distance(vp.consolidated_latitude, vp.consolidated_longitude, cp.latitude, cp.longitude) ASC
      LIMIT 1
    ) np ON true
    ORDER BY vp.updated_at DESC
    LIMIT p_limit
  ),
  recent_photos AS (
    SELECT 'photo'::TEXT AS content_type, cp.id::TEXT AS content_id,
           cp.caption AS caption, cp.street AS street,
           cp.worst_severity AS severity,
           (cp.detection_status = 'hidden')::BOOLEAN AS is_hidden,
           cp.created_at AS created_at, cp.image_url AS image_url,
           cp.latitude AS lat, cp.longitude AS lng,
           NULL::TEXT AS nearby_image_url, NULL::DOUBLE PRECISION AS nearby_m
    FROM community_photos cp
    ORDER BY cp.created_at DESC
    LIMIT p_limit
  )
  SELECT * FROM recent_potholes
  UNION ALL
  SELECT * FROM recent_photos
  ORDER BY created_at DESC
  LIMIT p_limit;
END;
$$;

-- Hide or unhide a single item (admin only)
DROP FUNCTION IF EXISTS admin_hide_content(TEXT, TEXT, BOOLEAN);
CREATE OR REPLACE FUNCTION admin_hide_content(
  p_content_type TEXT, p_content_id TEXT, p_hide BOOLEAN
)
RETURNS TABLE (is_hidden BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_hidden BOOLEAN := false;
BEGIN
  IF NOT is_admin() THEN RAISE EXCEPTION 'Not authorized'; END IF;
  IF p_content_type NOT IN ('pothole', 'photo') THEN RAISE EXCEPTION 'Bad content_type'; END IF;

  IF p_content_type = 'pothole' THEN
    IF p_hide THEN
      UPDATE verified_potholes
      SET caption = '[HIDDEN] ' || COALESCE(NULLIF(caption, ''), 'Pothole'),
          updated_at = now()
      WHERE id::TEXT = p_content_id
        AND (caption IS NULL OR caption NOT LIKE '[HIDDEN]%');
    ELSE
      UPDATE verified_potholes
      SET caption = regexp_replace(COALESCE(caption, ''), '^\[HIDDEN\]\s*', ''),
          updated_at = now()
      WHERE id::TEXT = p_content_id;
    END IF;
    SELECT (caption LIKE '[HIDDEN]%') INTO v_hidden
    FROM verified_potholes WHERE id::TEXT = p_content_id;
  ELSE
    IF p_hide THEN
      UPDATE community_photos
      SET detection_status = 'hidden', updated_at = now()
      WHERE id::TEXT = p_content_id;
    ELSE
      UPDATE community_photos
      SET detection_status = 'pending', updated_at = now()
      WHERE id::TEXT = p_content_id AND detection_status = 'hidden';
    END IF;
    SELECT (detection_status = 'hidden') INTO v_hidden
    FROM community_photos WHERE id::TEXT = p_content_id;
  END IF;

  is_hidden := COALESCE(v_hidden, p_hide);
  RETURN NEXT;
END;
$$;

GRANT EXECUTE ON FUNCTION is_admin() TO authenticated;
GRANT EXECUTE ON FUNCTION admin_list_recent(INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION admin_hide_content(TEXT, TEXT, BOOLEAN) TO authenticated;
