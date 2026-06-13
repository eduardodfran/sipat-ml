-- Add image_url column to verified_potholes table
-- This column stores the URL of the annotated frame image for each pothole

ALTER TABLE verified_potholes
ADD COLUMN IF NOT EXISTS image_url TEXT;
