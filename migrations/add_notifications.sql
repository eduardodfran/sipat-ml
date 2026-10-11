-- Migration: in-app notifications (bell) for comments, vote milestones,
-- verification taps, and fixed/active flips.
--
-- Design notes:
-- * All writes happen inside existing SECURITY DEFINER RPCs — clients never
--   INSERT directly (no INSERT policy).
-- * notify_owner() skips self-notifications (actor == owner → no row).
-- * Vote milestones fire on upvote crossings of multiples of 5 only.
-- * Re-crossing after an unvote can re-notify; accepted for v1.

-- =============================================================================
-- 1. notifications table + RLS
-- =============================================================================
CREATE TABLE IF NOT EXISTS notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('comment', 'vote_milestone', 'verify', 'flip')),
  content_type TEXT NOT NULL CHECK (content_type IN ('pothole', 'photo')),
  content_id TEXT NOT NULL,
  actor_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  actor_username TEXT,
  preview TEXT NOT NULL DEFAULT '',
  meta JSONB NOT NULL DEFAULT '{}'::JSONB,
  is_read BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_created
  ON notifications(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
  ON notifications(user_id, created_at DESC) WHERE is_read = false;

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own notifications" ON notifications;
CREATE POLICY "Users read own notifications"
  ON notifications FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users mark own notifications read" ON notifications;
CREATE POLICY "Users mark own notifications read"
  ON notifications FOR UPDATE USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- =============================================================================
-- 2. Helpers (server-side only; called from DEFINER RPCs)
-- =============================================================================
CREATE OR REPLACE FUNCTION get_post_owner_id(p_content_type TEXT, p_content_id TEXT)
RETURNS UUID
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
#variable_conflict use_column
BEGIN
  IF p_content_type = 'pothole' THEN
    RETURN (
      SELECT rm.user_id
      FROM verified_potholes vp
      JOIN rides_metadata rm ON rm.id = vp.ride_id
      WHERE vp.id::TEXT = p_content_id
      ORDER BY rm.created_at ASC
      LIMIT 1
    );
  ELSIF p_content_type = 'photo' THEN
    RETURN (SELECT cp.user_id FROM community_photos cp WHERE cp.id::TEXT = p_content_id);
  END IF;
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION notify_owner(
  p_owner_id UUID,
  p_actor_id UUID,
  p_kind TEXT,
  p_content_type TEXT,
  p_content_id TEXT,
  p_preview TEXT,
  p_meta JSONB DEFAULT '{}'::JSONB
)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
AS $$
#variable_conflict use_column
DECLARE
  v_actor_name TEXT;
BEGIN
  IF p_owner_id IS NULL OR p_owner_id = p_actor_id THEN
    RETURN;
  END IF;
  SELECT p.username INTO v_actor_name FROM profiles p WHERE p.id = p_actor_id;
  INSERT INTO notifications (user_id, kind, content_type, content_id, actor_user_id, actor_username, preview, meta)
  VALUES (p_owner_id, p_kind, p_content_type, p_content_id, p_actor_id,
          v_actor_name, COALESCE(p_preview, ''), COALESCE(p_meta, '{}'::JSONB));
END;
$$;

-- =============================================================================
-- 3. Hook: detection (pothole) comments
-- =============================================================================
DROP FUNCTION IF EXISTS create_detection_comment(INTEGER, TEXT);
CREATE OR REPLACE FUNCTION create_detection_comment(p_pothole_id INTEGER, p_body TEXT)
RETURNS TABLE (
    id UUID,
    body TEXT,
    created_at TIMESTAMPTZ,
    user_id TEXT,
    username TEXT,
    avatar_url TEXT
)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
#variable_conflict use_column
DECLARE
    v_user_id UUID := auth.uid();
    v_comment_id UUID;
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'Not authenticated';
    END IF;

    INSERT INTO detection_comments (pothole_id, user_id, body)
    VALUES (p_pothole_id, v_user_id, p_body)
    RETURNING id INTO v_comment_id;

    PERFORM notify_owner(
        get_post_owner_id('pothole', p_pothole_id::TEXT),
        v_user_id,
        'comment',
        'pothole',
        p_pothole_id::TEXT,
        LEFT(p_body, 120)
    );

    RETURN QUERY
    SELECT c.id, c.body, c.created_at, c.user_id::TEXT, p.username, p.avatar_url
    FROM detection_comments c
    LEFT JOIN profiles p ON p.id = c.user_id
    WHERE c.id = v_comment_id;
END;
$$;

-- =============================================================================
-- 4. Hook: community photo comments
-- =============================================================================
DROP FUNCTION IF EXISTS create_community_photo_comment(BIGINT, TEXT);
CREATE OR REPLACE FUNCTION create_community_photo_comment(p_photo_id BIGINT, p_body TEXT)
RETURNS TABLE (id UUID, body TEXT, created_at TIMESTAMPTZ, user_id TEXT, username TEXT, avatar_url TEXT)
LANGUAGE plpgsql SECURITY DEFINER AS $$
#variable_conflict use_column
DECLARE
  v_user_id UUID := auth.uid();
  v_comment_id UUID;
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated';
  END IF;
  INSERT INTO community_photo_comments (photo_id, user_id, body)
  VALUES (p_photo_id, v_user_id, p_body)
  RETURNING id INTO v_comment_id;

  PERFORM notify_owner(
      get_post_owner_id('photo', p_photo_id::TEXT),
      v_user_id,
      'comment',
      'photo',
      p_photo_id::TEXT,
      LEFT(p_body, 120)
  );

  RETURN QUERY
  SELECT c.id, c.body, c.created_at, c.user_id::TEXT, p.username, p.avatar_url
  FROM community_photo_comments c
  LEFT JOIN profiles p ON p.id = c.user_id
  WHERE c.id = v_comment_id;
END;
$$;

-- =============================================================================
-- 5. Hook: verification taps + fixed/active flips
-- =============================================================================
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
  v_old_status TEXT := 'active';
  v_actor_name TEXT;
BEGIN
  IF v_user_id IS NULL THEN RAISE EXCEPTION 'Not authenticated'; END IF;
  IF p_content_type NOT IN ('pothole', 'photo') THEN RAISE EXCEPTION 'Bad content_type'; END IF;
  IF p_signal NOT IN ('still', 'fixed') THEN RAISE EXCEPTION 'Bad signal'; END IF;

  IF p_content_type = 'pothole' THEN
    SELECT vp.activity_status INTO v_old_status FROM verified_potholes vp WHERE vp.id::TEXT = p_content_id;
  ELSE
    SELECT cp.activity_status INTO v_old_status FROM community_photos cp WHERE cp.id::TEXT = p_content_id;
  END IF;
  v_old_status := COALESCE(v_old_status, 'active');

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

  SELECT p.username INTO v_actor_name FROM profiles p WHERE p.id = v_user_id;
  v_actor_name := COALESCE(v_actor_name, 'Someone');

  PERFORM notify_owner(
      get_post_owner_id(p_content_type, p_content_id),
      v_user_id,
      'verify',
      p_content_type,
      p_content_id,
      v_actor_name || ' tapped ' || CASE WHEN p_signal = 'fixed' THEN 'Fixed' ELSE 'Still here' END,
      jsonb_build_object('signal', p_signal, 'fixed', v_fixed, 'still', v_still)
  );

  IF v_old_status <> v_status THEN
    PERFORM notify_owner(
        get_post_owner_id(p_content_type, p_content_id),
        v_user_id,
        'flip',
        p_content_type,
        p_content_id,
        CASE WHEN v_status = 'fixed'
             THEN 'Your post was marked Fixed and hidden from feeds'
             ELSE 'Your post was contested back to active' END,
        jsonb_build_object('from', v_old_status, 'to', v_status, 'fixed', v_fixed, 'still', v_still)
    );
  END IF;

  fixed_count := v_fixed; still_count := v_still; activity_status := v_status;
  RETURN NEXT;
END;
$$;

-- =============================================================================
-- 6. Hook: vote milestones (upvotes crossing multiples of 5)
-- =============================================================================
DROP FUNCTION IF EXISTS vote_content(TEXT, TEXT, SMALLINT);
CREATE OR REPLACE FUNCTION vote_content(p_content_type TEXT, p_content_id TEXT, p_vote_value SMALLINT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID := auth.uid();
  v_upvotes BIGINT := 0;
BEGIN
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated';
  END IF;

  INSERT INTO content_votes (user_id, content_type, content_id, vote_value)
  VALUES (v_user_id, p_content_type, p_content_id, p_vote_value)
  ON CONFLICT (user_id, content_type, content_id)
  DO UPDATE SET vote_value = p_vote_value, created_at = now();

  IF p_vote_value = 1 AND p_content_type IN ('pothole', 'photo') THEN
    SELECT COUNT(*) FILTER (WHERE cv.vote_value = 1) INTO v_upvotes
    FROM content_votes cv
    WHERE cv.content_type = p_content_type AND cv.content_id = p_content_id;
    IF v_upvotes > 0 AND (v_upvotes % 5) = 0 THEN
      PERFORM notify_owner(
          get_post_owner_id(p_content_type, p_content_id),
          v_user_id,
          'vote_milestone',
          p_content_type,
          p_content_id,
          'Your post reached ' || v_upvotes || ' upvotes',
          jsonb_build_object('upvotes', v_upvotes)
      );
    END IF;
  END IF;

  RETURN QUERY
  SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

-- =============================================================================
-- 7. Grants + realtime + migration record
-- =============================================================================
GRANT EXECUTE ON FUNCTION get_detection_comments(INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION create_detection_comment(INTEGER, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION get_community_photo_comments(BIGINT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION create_community_photo_comment(BIGINT, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION mark_hazard_signal(TEXT, TEXT, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION get_hazard_verification(TEXT, TEXT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION vote_content(TEXT, TEXT, SMALLINT) TO authenticated;

DO $$
BEGIN
  ALTER PUBLICATION supabase_realtime ADD TABLE notifications;
EXCEPTION
  WHEN undefined_object OR duplicate_object THEN
    NULL;
END $$;

DO $$
BEGIN
  INSERT INTO _migrations (filename, applied_at)
  VALUES ('add_notifications.sql', now())
  ON CONFLICT (filename) DO NOTHING;
EXCEPTION
  WHEN undefined_table OR undefined_object THEN
    NULL;
END $$;
