-- Migration: Add performance indexes for common query patterns
-- Optimizes: user ride lookups, status filters, detection joins, pothole severity sorts

-- rides_metadata: user ride history, status filtering, ordering by creation time
CREATE INDEX IF NOT EXISTS idx_rides_metadata_user_id ON rides_metadata(user_id);
CREATE INDEX IF NOT EXISTS idx_rides_metadata_status ON rides_metadata(status);
CREATE INDEX IF NOT EXISTS idx_rides_metadata_user_status ON rides_metadata(user_id, status);
CREATE INDEX IF NOT EXISTS idx_rides_metadata_created_at ON rides_metadata(created_at DESC);

-- raw_detections: join key to rides_metadata for detection queries
CREATE INDEX IF NOT EXISTS idx_raw_detections_ride_id ON raw_detections(ride_id);

-- verified_potholes: severity filtering and recency sorting
CREATE INDEX IF NOT EXISTS idx_verified_potholes_worst_severity ON verified_potholes(worst_severity);
CREATE INDEX IF NOT EXISTS idx_verified_potholes_updated_at ON verified_potholes(updated_at DESC);
