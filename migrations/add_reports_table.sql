-- Migration: Add content_reports table for community photo / pothole moderation
-- Creates table, RLS policies, and RPC functions for reporting and unreporting content.

-- 1. Table
CREATE TABLE IF NOT EXISTS content_reports (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  content_type TEXT NOT NULL CHECK (content_type IN ('photo', 'pothole')),
  content_id TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('spam', 'inappropriate', 'not_pothole', 'duplicate', 'other')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, content_type, content_id)
);

-- 2. Indexes
CREATE INDEX IF NOT EXISTS idx_content_reports_content ON content_reports(content_type, content_id);
CREATE INDEX IF NOT EXISTS idx_content_reports_user ON content_reports(user_id);

-- 3. RLS
ALTER TABLE content_reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view their own reports" ON content_reports;
CREATE POLICY "Users can view their own reports"
  ON content_reports FOR SELECT
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Authenticated users can insert reports" ON content_reports;
CREATE POLICY "Authenticated users can insert reports"
  ON content_reports FOR INSERT
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can delete their own reports" ON content_reports;
CREATE POLICY "Users can delete their own reports"
  ON content_reports FOR DELETE
  USING (auth.uid() = user_id);

-- 4. RPC: report_content
DROP FUNCTION IF EXISTS report_content(TEXT, TEXT, TEXT);
CREATE OR REPLACE FUNCTION report_content(
  p_content_type TEXT,
  p_content_id   TEXT,
  p_reason       TEXT
)
RETURNS TABLE (report_count BIGINT, is_hidden BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
  v_count   BIGINT;
  v_hidden  BOOLEAN;
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated';
  END IF;

  IF p_content_type NOT IN ('photo', 'pothole') THEN
    RAISE EXCEPTION 'Invalid content_type: %', p_content_type;
  END IF;

  IF p_reason NOT IN ('spam', 'inappropriate', 'not_pothole', 'duplicate', 'other') THEN
    RAISE EXCEPTION 'Invalid reason: %', p_reason;
  END IF;

  -- Upsert (insert or update reason if user already reported this content)
  INSERT INTO content_reports (user_id, content_type, content_id, reason)
  VALUES (v_user_id, p_content_type, p_content_id, p_reason)
  ON CONFLICT (user_id, content_type, content_id)
  DO UPDATE SET reason = EXCLUDED.reason, created_at = now();

  -- Count total reports for this content
  SELECT count(*) INTO v_count
  FROM content_reports
  WHERE content_type = p_content_type AND content_id = p_content_id;

  v_hidden := (v_count >= 3);

  -- Auto-hide when threshold is reached
  IF v_hidden THEN
    IF p_content_type = 'photo' THEN
      UPDATE community_photos
      SET detection_status = 'hidden'
      WHERE id = p_content_id::BIGINT;
    ELSIF p_content_type = 'pothole' THEN
      UPDATE verified_potholes
      SET caption = '[HIDDEN] ' || COALESCE(caption, '')
      WHERE id = p_content_id::INTEGER
        AND (caption IS NULL OR caption NOT LIKE '[HIDDEN]%');
    END IF;
  END IF;

  report_count := v_count;
  is_hidden    := v_hidden;
  RETURN NEXT;
END;
$$;

-- 5. RPC: has_user_reported
DROP FUNCTION IF EXISTS has_user_reported(TEXT, TEXT);
CREATE OR REPLACE FUNCTION has_user_reported(
  p_content_type TEXT,
  p_content_id   TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
  v_exists  BOOLEAN;
BEGIN
  IF v_user_id IS NULL THEN
    RETURN false;
  END IF;

  SELECT EXISTS(
    SELECT 1 FROM content_reports
    WHERE user_id = v_user_id
      AND content_type = p_content_type
      AND content_id   = p_content_id
  ) INTO v_exists;

  RETURN v_exists;
END;
$$;

-- 6. RPC: unreport_content
DROP FUNCTION IF EXISTS unreport_content(TEXT, TEXT);
CREATE OR REPLACE FUNCTION unreport_content(
  p_content_type TEXT,
  p_content_id   TEXT
)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
  v_count   BIGINT;
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated';
  END IF;

  DELETE FROM content_reports
  WHERE user_id      = v_user_id
    AND content_type = p_content_type
    AND content_id   = p_content_id;

  -- If content was hidden, re-show it when reports drop below threshold
  SELECT count(*) INTO v_count
  FROM content_reports
  WHERE content_type = p_content_type AND content_id = p_content_id;

  IF v_count < 3 THEN
    IF p_content_type = 'photo' THEN
      UPDATE community_photos
      SET detection_status = 'pending'
      WHERE id = p_content_id::BIGINT AND detection_status = 'hidden';
    ELSIF p_content_type = 'pothole' THEN
      UPDATE verified_potholes
      SET caption = regexp_replace(caption, '^\[HIDDEN\]\s*', '')
      WHERE id = p_content_id::INTEGER
        AND caption LIKE '[HIDDEN]%';
    END IF;
  END IF;
END;
$$;

-- 7. Grants
GRANT EXECUTE ON FUNCTION report_content(TEXT, TEXT, TEXT)   TO authenticated;
GRANT EXECUTE ON FUNCTION has_user_reported(TEXT, TEXT)      TO authenticated;
GRANT EXECUTE ON FUNCTION unreport_content(TEXT, TEXT)       TO authenticated;
