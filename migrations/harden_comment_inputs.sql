-- Harden user-generated comment inputs (XSS follow-up, Sep 1 2026 incident).
-- Enforces non-empty, max 500-char bodies at the table level so ANY writer
-- (current or future RPC versions, service role, scripts) is constrained.
-- Run this in the Supabase SQL Editor. Safe to re-run (drops constraint first).

-- Preview: should return 0 rows before the constraints are added
select id, photo_id, char_length(body) as len, left(body, 80) as preview
from public.community_photo_comments
where char_length(btrim(body)) < 1 or char_length(body) > 500;

select id, pothole_id, char_length(body) as len, left(body, 80) as preview
from public.detection_comments
where char_length(btrim(body)) < 1 or char_length(body) > 500;

alter table public.community_photo_comments
  drop constraint if exists community_photo_comments_body_length;
alter table public.community_photo_comments
  add constraint community_photo_comments_body_length
  check (char_length(btrim(body)) between 1 and 500);

alter table public.detection_comments
  drop constraint if exists detection_comments_body_length;
alter table public.detection_comments
  add constraint detection_comments_body_length
  check (char_length(btrim(body)) between 1 and 500);

-- Verify constraints exist
select conname, conrelid::regclass as table_name
from pg_constraint
where conname in ('community_photo_comments_body_length', 'detection_comments_body_length');
