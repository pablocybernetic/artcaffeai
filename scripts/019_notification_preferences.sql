-- ============================================================
-- ARTCAFFE — Per-user notification preferences
--
-- Adds an opt-out preferences store to team_members, keyed by the same
-- `type` strings notification_service.py already uses:
--   ideation_complete, production_complete, approval_needed,
--   post_scheduled, post_published, reminders
--
-- Opt-out model: a member with no key for a given type (or no row at
-- all) still gets notified — application code treats a missing key as
-- true. The column default below just makes that explicit and
-- queryable from day one for every existing member.
-- ============================================================

ALTER TABLE public.team_members
  ADD COLUMN IF NOT EXISTS notification_preferences jsonb NOT NULL DEFAULT '{
    "ideation_complete": true,
    "production_complete": true,
    "approval_needed": true,
    "post_scheduled": true,
    "post_published": true,
    "reminders": true
  }'::jsonb;

-- Backfill existing rows that predate the column default.
UPDATE public.team_members
SET notification_preferences = '{
  "ideation_complete": true,
  "production_complete": true,
  "approval_needed": true,
  "post_scheduled": true,
  "post_published": true,
  "reminders": true
}'::jsonb
WHERE notification_preferences IS NULL OR notification_preferences = '{}'::jsonb;
