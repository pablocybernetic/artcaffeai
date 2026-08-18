-- ============================================================
-- ARTCAFFE — Generic admin-configurable settings store
--
-- Backs the Settings → Agents "Automation schedules" card: cron
-- enabled flags, intervals, and hour windows for master_scheduler,
-- reminder_scheduler, and meta_sync_scheduler need to survive
-- process restarts and VM redeploys, not just live in each module's
-- in-memory _state dict seeded from env vars on boot.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.app_settings (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.app_settings ENABLE ROW LEVEL SECURITY;
-- Service-role only (bypasses RLS entirely) — no authenticated/anon
-- client should ever touch this table, so no policies are defined.
