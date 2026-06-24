-- Create pothole_comments table
CREATE TABLE IF NOT EXISTS pothole_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pothole_id INTEGER NOT NULL REFERENCES verified_potholes(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pothole_comments_pothole_id ON pothole_comments(pothole_id);

ALTER TABLE pothole_comments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Anyone can view comments" ON pothole_comments;
DROP POLICY IF EXISTS "Authenticated users can insert comments" ON pothole_comments;

-- Users can read all comments
CREATE POLICY "Anyone can view comments"
    ON pothole_comments FOR SELECT
    USING (true);

-- Users can insert their own comments (auth.uid() check is implicit via SECURITY DEFINER RPC)
CREATE POLICY "Authenticated users can insert comments"
    ON pothole_comments FOR INSERT
    WITH CHECK (auth.uid() = user_id);

-- RPC: get comments for a pothole
CREATE OR REPLACE FUNCTION get_pothole_comments(p_pothole_id INTEGER)
RETURNS TABLE (
    id UUID,
    body TEXT,
    created_at TIMESTAMPTZ,
    user_id TEXT,
    username TEXT,
    avatar_url TEXT
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT c.id, c.body, c.created_at, c.user_id::TEXT, p.username, p.avatar_url
    FROM pothole_comments c
    LEFT JOIN profiles p ON p.id = c.user_id
    WHERE c.pothole_id = p_pothole_id
    ORDER BY c.created_at ASC;
END;
$$;

-- RPC: create a comment (user_id derived from auth)
CREATE OR REPLACE FUNCTION create_pothole_comment(p_pothole_id INTEGER, p_body TEXT)
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
DECLARE
    v_user_id UUID := auth.uid();
    v_comment_id UUID;
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'Not authenticated';
    END IF;

    INSERT INTO pothole_comments (pothole_id, user_id, body)
    VALUES (p_pothole_id, v_user_id, p_body)
    RETURNING id INTO v_comment_id;

    RETURN QUERY
    SELECT c.id, c.body, c.created_at, c.user_id::TEXT, p.username, p.avatar_url
    FROM pothole_comments c
    LEFT JOIN profiles p ON p.id = c.user_id
    WHERE c.id = v_comment_id;
END;
$$;

GRANT EXECUTE ON FUNCTION get_pothole_comments(INTEGER) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION create_pothole_comment(INTEGER, TEXT) TO authenticated;
