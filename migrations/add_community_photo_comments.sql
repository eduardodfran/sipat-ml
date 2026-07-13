CREATE TABLE IF NOT EXISTS community_photo_comments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  photo_id BIGINT NOT NULL REFERENCES community_photos(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_community_photo_comments_photo_id ON community_photo_comments(photo_id);

ALTER TABLE community_photo_comments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Anyone can view community photo comments" ON community_photo_comments;
CREATE POLICY "Anyone can view community photo comments"
  ON community_photo_comments FOR SELECT USING (true);

DROP POLICY IF EXISTS "Authenticated users can create community photo comments" ON community_photo_comments;
CREATE POLICY "Authenticated users can create community photo comments"
  ON community_photo_comments FOR INSERT
  WITH CHECK (auth.uid() = user_id);

DROP FUNCTION IF EXISTS get_community_photo_comments(BIGINT);
CREATE OR REPLACE FUNCTION get_community_photo_comments(p_photo_id BIGINT)
RETURNS TABLE (id UUID, body TEXT, created_at TIMESTAMPTZ, user_id TEXT, username TEXT, avatar_url TEXT)
LANGUAGE plpgsql STABLE SECURITY DEFINER AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
  SELECT c.id, c.body, c.created_at, c.user_id::TEXT, p.username, p.avatar_url
  FROM community_photo_comments c
  LEFT JOIN profiles p ON p.id = c.user_id
  WHERE c.photo_id = p_photo_id
  ORDER BY c.created_at ASC;
END;
$$;

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
  RETURN QUERY
  SELECT c.id, c.body, c.created_at, c.user_id::TEXT, p.username, p.avatar_url
  FROM community_photo_comments c
  LEFT JOIN profiles p ON p.id = c.user_id
  WHERE c.id = v_comment_id;
END;
$$;

GRANT EXECUTE ON FUNCTION get_community_photo_comments(BIGINT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION create_community_photo_comment(BIGINT, TEXT) TO authenticated;
