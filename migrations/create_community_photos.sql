CREATE TABLE IF NOT EXISTS community_photos (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  image_url TEXT NOT NULL,
  latitude DOUBLE PRECISION NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  street TEXT,
  barangay TEXT,
  city TEXT,
  province TEXT,
  region TEXT,
  country TEXT,
  formatted_address TEXT,
  address_geocoded_at TIMESTAMPTZ,
  detection_status TEXT NOT NULL DEFAULT 'pending',
  worst_severity TEXT,
  confidence DOUBLE PRECISION,
  class_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_community_photos_user ON community_photos(user_id);
CREATE INDEX idx_community_photos_created ON community_photos(created_at DESC);

CREATE OR REPLACE VIEW v_community_photos AS
SELECT
  cp.*,
  p.username AS reporter_username,
  p.avatar_url AS reporter_avatar
FROM community_photos cp
LEFT JOIN profiles p ON p.id = cp.user_id;

ALTER TABLE community_photos ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Community photos are viewable by everyone"
  ON community_photos FOR SELECT USING (true);

CREATE POLICY "Users can insert own photos"
  ON community_photos FOR INSERT
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Service role can update any photo"
  ON community_photos FOR UPDATE
  USING (true)
  WITH CHECK (true);
