-- Enable PostGIS extension (idempotent)
CREATE EXTENSION IF NOT EXISTS postgis;

-- Add a geometry(Point, 4326) column if it does not exist
ALTER TABLE verified_potholes ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326);

-- Populate the geometry column from existing lat/lng coordinates
UPDATE verified_potholes
SET geom = ST_SetSRID(ST_MakePoint(consolidated_longitude, consolidated_latitude), 4326)
WHERE geom IS NULL
  AND consolidated_latitude IS NOT NULL
  AND consolidated_longitude IS NOT NULL;

-- Create a GiST spatial index for efficient ST_DWithin / nearest-neighbor queries
CREATE INDEX IF NOT EXISTS idx_verified_potholes_geom
  ON verified_potholes
  USING GIST (geom);
