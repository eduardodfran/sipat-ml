SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '60s';

LOCK TABLE verified_potholes, detection_comments, pothole_comments,
    content_votes, content_reports, hazard_verifications, notifications
    IN SHARE ROW EXCLUSIVE MODE;

DROP TABLE IF EXISTS pg_temp._dupe_map;

CREATE TEMP TABLE _dupe_map ON COMMIT DROP AS
SELECT
    vp.id AS duplicate_id,
    grouped.survivor_id
FROM verified_potholes vp
JOIN (
    SELECT
        consolidated_latitude,
        consolidated_longitude,
        MIN(id) AS survivor_id
    FROM verified_potholes
    GROUP BY consolidated_latitude, consolidated_longitude
    HAVING COUNT(*) > 1
) grouped
    ON vp.consolidated_latitude = grouped.consolidated_latitude
    AND vp.consolidated_longitude = grouped.consolidated_longitude
WHERE vp.id <> grouped.survivor_id;

DO $$
DECLARE
    group_count INTEGER;
    pair_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO group_count
    FROM (
        SELECT 1
        FROM verified_potholes
        GROUP BY consolidated_latitude, consolidated_longitude
        HAVING COUNT(*) > 1
    ) grouped;

    SELECT COUNT(*) INTO pair_count FROM pg_temp._dupe_map;

    IF group_count <> 0 OR pair_count <> 0 THEN
        IF group_count <> 33 OR pair_count <> 33 THEN
            RAISE EXCEPTION
                'Dedupe preflight mismatch: groups=%, duplicate_rows=%',
                group_count, pair_count;
        END IF;
    END IF;
END $$;

UPDATE detection_comments AS comment
SET pothole_id = duplicate.survivor_id
FROM pg_temp._dupe_map AS duplicate
WHERE comment.pothole_id = duplicate.duplicate_id;

UPDATE pothole_comments AS comment
SET pothole_id = duplicate.survivor_id
FROM pg_temp._dupe_map AS duplicate
WHERE comment.pothole_id = duplicate.duplicate_id;

UPDATE notifications AS notification
SET content_id = duplicate.survivor_id::TEXT
FROM pg_temp._dupe_map AS duplicate
WHERE notification.content_type = 'pothole'
    AND notification.content_id = duplicate.duplicate_id::TEXT;

INSERT INTO hazard_verifications (user_id, content_type, content_id, signal, updated_at)
SELECT
    verification.user_id,
    verification.content_type,
    duplicate.survivor_id::TEXT,
    verification.signal,
    verification.updated_at
FROM hazard_verifications AS verification
JOIN pg_temp._dupe_map AS duplicate
    ON verification.content_id = duplicate.duplicate_id::TEXT
WHERE verification.content_type = 'pothole'
ON CONFLICT (user_id, content_type, content_id)
DO UPDATE SET
    signal = EXCLUDED.signal,
    updated_at = EXCLUDED.updated_at
WHERE EXCLUDED.updated_at > hazard_verifications.updated_at;

DELETE FROM hazard_verifications AS verification
USING pg_temp._dupe_map AS duplicate
WHERE verification.content_type = 'pothole'
    AND verification.content_id = duplicate.duplicate_id::TEXT;

INSERT INTO content_votes (user_id, content_type, content_id, vote_value, created_at)
SELECT
    vote.user_id,
    vote.content_type,
    duplicate.survivor_id::TEXT,
    vote.vote_value,
    vote.created_at
FROM content_votes AS vote
JOIN pg_temp._dupe_map AS duplicate
    ON vote.content_id = duplicate.duplicate_id::TEXT
WHERE vote.content_type = 'pothole'
ON CONFLICT (user_id, content_type, content_id)
DO UPDATE SET
    vote_value = EXCLUDED.vote_value,
    created_at = EXCLUDED.created_at
WHERE EXCLUDED.created_at > content_votes.created_at;

DELETE FROM content_votes AS vote
USING pg_temp._dupe_map AS duplicate
WHERE vote.content_type = 'pothole'
    AND vote.content_id = duplicate.duplicate_id::TEXT;

INSERT INTO content_reports (user_id, content_type, content_id, reason, created_at)
SELECT
    report.user_id,
    report.content_type,
    duplicate.survivor_id::TEXT,
    report.reason,
    report.created_at
FROM content_reports AS report
JOIN pg_temp._dupe_map AS duplicate
    ON report.content_id = duplicate.duplicate_id::TEXT
WHERE report.content_type = 'pothole'
ON CONFLICT (user_id, content_type, content_id)
DO UPDATE SET
    reason = EXCLUDED.reason,
    created_at = EXCLUDED.created_at
WHERE EXCLUDED.created_at > content_reports.created_at;

DELETE FROM content_reports AS report
USING pg_temp._dupe_map AS duplicate
WHERE report.content_type = 'pothole'
    AND report.content_id = duplicate.duplicate_id::TEXT;

WITH affected AS (
    SELECT DISTINCT survivor_id
    FROM pg_temp._dupe_map
),
signal_counts AS (
    SELECT
        affected.survivor_id,
        COUNT(*) FILTER (WHERE verification.signal = 'fixed')::INTEGER AS fixed_count,
        COUNT(*) FILTER (WHERE verification.signal = 'still')::INTEGER AS still_count
    FROM affected
    LEFT JOIN hazard_verifications AS verification
        ON verification.content_type = 'pothole'
        AND verification.content_id = affected.survivor_id::TEXT
    GROUP BY affected.survivor_id
)
UPDATE verified_potholes AS pothole
SET
    fixed_count = signal_counts.fixed_count,
    still_count = signal_counts.still_count,
    activity_status = CASE
        WHEN signal_counts.fixed_count >= 3
            AND signal_counts.fixed_count > signal_counts.still_count
        THEN 'fixed'
        ELSE 'active'
    END,
    updated_at = now()
FROM signal_counts
WHERE pothole.id = signal_counts.survivor_id
    AND (
        pothole.fixed_count <> signal_counts.fixed_count
        OR pothole.still_count <> signal_counts.still_count
        OR pothole.activity_status <> CASE
            WHEN signal_counts.fixed_count >= 3
                AND signal_counts.fixed_count > signal_counts.still_count
            THEN 'fixed'
            ELSE 'active'
        END
    );

DELETE FROM verified_potholes AS pothole
USING pg_temp._dupe_map AS duplicate
WHERE pothole.id = duplicate.duplicate_id;

CREATE UNIQUE INDEX IF NOT EXISTS verified_potholes_exact_coordinates_uidx
ON verified_potholes (consolidated_latitude, consolidated_longitude);
