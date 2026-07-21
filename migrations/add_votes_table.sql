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
DROP POLICY IF EXISTS "Authenticated users can insert votes" ON content_votes;
DROP POLICY IF EXISTS "Authenticated users can update own votes" ON content_votes;
DROP POLICY IF EXISTS "Authenticated users can delete own votes" ON content_votes;

CREATE POLICY "Anyone can view votes"
  ON content_votes FOR SELECT USING (true);

CREATE POLICY "Authenticated users can insert votes"
  ON content_votes FOR INSERT
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Authenticated users can update own votes"
  ON content_votes FOR UPDATE
  USING (auth.uid() = user_id);

CREATE POLICY "Authenticated users can delete own votes"
  ON content_votes FOR DELETE
  USING (auth.uid() = user_id);

-- Helper: get current vote counts and user's vote
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
    (SELECT cv2.vote_value FROM content_votes cv2
     WHERE cv2.content_type = p_content_type
       AND cv2.content_id = p_content_id
       AND cv2.user_id = auth.uid()) AS user_vote
  FROM content_votes cv
  WHERE cv.content_type = p_content_type
    AND cv.content_id = p_content_id;
END;
$$;

-- Upsert a vote and return updated counts
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
  DO UPDATE SET vote_value = EXCLUDED.vote_value, created_at = now();

  RETURN QUERY
  SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

-- Remove a vote and return updated counts
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

  RETURN QUERY
  SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

GRANT EXECUTE ON FUNCTION get_content_votes(TEXT, TEXT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION vote_content(TEXT, TEXT, SMALLINT) TO authenticated;
GRANT EXECUTE ON FUNCTION unvote_content(TEXT, TEXT) TO authenticated;
