-- Prerequisite: Create exec_sql function for migration runner
-- Run this once via Supabase SQL Editor before using the migration runner

CREATE OR REPLACE FUNCTION exec_sql(query TEXT)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    EXECUTE query;
END;
$$;
