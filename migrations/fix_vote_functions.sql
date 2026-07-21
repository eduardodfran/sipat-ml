-- Fix: Create vote functions from scratch
-- Run this in Supabase SQL Editor

-- Drop existing functions if any (handle multiple overloads)
DO $$
BEGIN
  DROP FUNCTION IF EXISTS get_content_votes(p_content_type TEXT, p_content_id TEXT);
  DROP FUNCTION IF EXISTS vote_content(p_content_type TEXT, p_content_id TEXT, p_vote_value SMALLINT);
  DROP FUNCTION IF EXISTS unvote_content(p_content_type TEXT, p_content_id TEXT);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

-- Function to get votes
CREATE OR REPLACE FUNCTION get_content_votes(p_content_type TEXT, p_content_id TEXT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql
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

-- Function to add/update vote
CREATE OR REPLACE FUNCTION vote_content(p_content_type TEXT, p_content_id TEXT, p_vote_value SMALLINT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql
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

  RETURN QUERY
  SELECT * FROM get_content_votes(p_content_type, p_content_id);
END;
$$;

-- Function to remove vote
CREATE OR REPLACE FUNCTION unvote_content(p_content_type TEXT, p_content_id TEXT)
RETURNS TABLE (upvotes BIGINT, downvotes BIGINT, net_score BIGINT, user_vote SMALLINT)
LANGUAGE plpgsql
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

-- Grant permissions
GRANT EXECUTE ON FUNCTION get_content_votes(TEXT, TEXT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION vote_content(TEXT, TEXT, SMALLINT) TO authenticated;
GRANT EXECUTE ON FUNCTION unvote_content(TEXT, TEXT) TO authenticated;
