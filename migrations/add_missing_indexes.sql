-- Migration: Add missing indexes for common query patterns
-- Addresses: community_photo status filters, distress summary grouping,
--             pothole spatial lookups, and raw_detection updates

-- community_photos: status filtering on feed/map
CREATE INDEX IF NOT EXISTS idx_community_photos_detection_status ON community_photos(detection_status);

-- verified_potholes: spatial lookup for clustering (nearest-neighbor in worker.py)
CREATE INDEX IF NOT EXISTS idx_verified_potholes_lat_lng ON verified_potholes(consolidated_latitude, consolidated_longitude);

-- raw_detections: distress summary grouping by class_name + severity
CREATE INDEX IF NOT EXISTS idx_raw_detections_class_severity ON raw_detections(class_name, severity);

-- raw_detections: status-based filtering
CREATE INDEX IF NOT EXISTS idx_raw_detections_class_name ON raw_detections(class_name);