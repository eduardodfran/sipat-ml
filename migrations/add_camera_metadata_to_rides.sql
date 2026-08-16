-- Add camera metadata columns to rides_metadata.
-- These fields let the backend correctly scale the generic 1920x1080 camera
-- intrinsics (fx/fy/cx/cy) to the actual recording resolution + zoom level,
-- which is critical for accurate IPM area estimates and severity classification.
--
-- Run this BEFORE recording any new rides, otherwise zoom_factor defaults to 1.0.

ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS video_width       INTEGER;
ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS video_height      INTEGER;
ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS focal_length_35mm REAL;
ALTER TABLE rides_metadata ADD COLUMN IF NOT EXISTS zoom_factor       REAL DEFAULT 1.0;

COMMENT ON COLUMN rides_metadata.video_width       IS 'Captured video width in pixels (pre-crop). Used to scale fx/cx.';
COMMENT ON COLUMN rides_metadata.video_height      IS 'Captured video height in pixels (pre-crop). Used to scale fy/cy.';
COMMENT ON COLUMN rides_metadata.focal_length_35mm IS 'Optional 35mm-equivalent focal length from EXIF. If provided, overrides generic fx/fy.';
COMMENT ON COLUMN rides_metadata.zoom_factor       IS 'Effective digital + optical zoom multiplier (1.0 = wide). Multiplies fx/fy.';

CREATE INDEX IF NOT EXISTS idx_rides_metadata_zoom_factor ON rides_metadata(zoom_factor);
