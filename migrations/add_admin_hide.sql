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
  severity TEXT, is_hidden BOOLEAN, created_at TIMESTAMPTZ, image_url TEXT
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
  IF NOT is_admin() THEN RAISE EXCEPTION 'Not authorized'; END IF;

  RETURN QUERY
  (SELECT 'pothole'::TEXT, vp.id::TEXT, vp.caption, vp.street, vp.worst_severity,
          (vp.caption LIKE '[HIDDEN]%')::BOOLEAN, vp.updated_at, vp.image_url
   FROM verified_potholes vp
   ORDER BY vp.updated_at DESC
   LIMIT p_limit)
  UNION ALL
  (SELECT 'photo'::TEXT, cp.id::TEXT, cp.caption, cp.street, cp.worst_severity,
          (cp.detection_status = 'hidden')::BOOLEAN, cp.created_at, cp.image_url
   FROM community_photos cp
   ORDER BY cp.created_at DESC
   LIMIT p_limit)
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
