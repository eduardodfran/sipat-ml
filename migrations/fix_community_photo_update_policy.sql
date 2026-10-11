-- Fix: scope community_photos UPDATE to row owners.
-- Previously "Service role can update any photo" used USING (true) WITH CHECK (true)
-- with no TO clause, so it applied to anon/authenticated: anyone could rewrite any
-- photo row. Service role bypasses RLS, so backend flows are unaffected.
-- Paste via Supabase SQL Editor.

DROP POLICY IF EXISTS "Service role can update any photo" ON community_photos;

DROP POLICY IF EXISTS "Users can update own photos" ON community_photos;
CREATE POLICY "Users can update own photos"
  ON community_photos FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

DO $$
BEGIN
  INSERT INTO _migrations (filename, applied_at)
  VALUES ('fix_community_photo_update_policy.sql', now())
  ON CONFLICT (filename) DO NOTHING;
EXCEPTION
  WHEN undefined_table OR undefined_object THEN
    NULL;
END $$;
