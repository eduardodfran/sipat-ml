-- Migration: Add reverse-geocoded address columns to verified_potholes
-- Converts lat/lng to human-readable addresses via Nominatim (cached in DB)

ALTER TABLE verified_potholes
    ADD COLUMN IF NOT EXISTS street TEXT,
    ADD COLUMN IF NOT EXISTS barangay TEXT,
    ADD COLUMN IF NOT EXISTS city TEXT,
    ADD COLUMN IF NOT EXISTS province TEXT,
    ADD COLUMN IF NOT EXISTS region TEXT,
    ADD COLUMN IF NOT EXISTS country TEXT,
    ADD COLUMN IF NOT EXISTS formatted_address TEXT,
    ADD COLUMN IF NOT EXISTS address_geocoded_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_verified_potholes_geocoded
    ON verified_potholes (address_geocoded_at)
    WHERE address_geocoded_at IS NULL;
