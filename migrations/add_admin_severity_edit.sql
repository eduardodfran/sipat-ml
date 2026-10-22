DROP FUNCTION IF EXISTS admin_set_severity(TEXT, TEXT, TEXT);

CREATE OR REPLACE FUNCTION admin_set_severity(
    p_content_type TEXT,
    p_content_id TEXT,
    p_severity TEXT
)
RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    v_severity TEXT;
BEGIN
    IF NOT is_admin() THEN
        RAISE EXCEPTION 'Not authorized';
    END IF;

    IF p_content_type NOT IN ('pothole', 'photo') THEN
        RAISE EXCEPTION 'Bad content_type';
    END IF;

    IF p_severity NOT IN ('Severe', 'Moderate', 'Minor', 'Unknown') THEN
        RAISE EXCEPTION 'Bad severity';
    END IF;

    IF p_content_type = 'pothole' THEN
        UPDATE verified_potholes
        SET worst_severity = p_severity
        WHERE id::TEXT = p_content_id
        RETURNING worst_severity INTO v_severity;
    ELSE
        UPDATE community_photos
        SET worst_severity = p_severity
        WHERE id::TEXT = p_content_id
        RETURNING worst_severity INTO v_severity;
    END IF;

    IF v_severity IS NULL THEN
        RAISE EXCEPTION 'Content not found: % %', p_content_type, p_content_id;
    END IF;

    RETURN v_severity;
END;
$$;

REVOKE ALL ON FUNCTION admin_set_severity(TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_set_severity(TEXT, TEXT, TEXT) TO authenticated;

NOTIFY pgrst, 'reload schema';
