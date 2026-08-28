-- ============================================================
-- ARTCAFFE — Location sync logs
--
-- One row per Google Places sync attempt (manual "Sync Now"/"Sync All"
-- or the scheduled cron in locations_scheduler.py), for the admin
-- /locations/sync-history page.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.location_sync_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id uuid REFERENCES public.locations(id) ON DELETE CASCADE,
  provider text NOT NULL DEFAULT 'google',
  status text NOT NULL CHECK (status IN ('queued', 'syncing', 'completed', 'failed')),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  fields_changed jsonb NOT NULL DEFAULT '[]'::jsonb,
  error_message text,
  response_code integer,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS location_sync_logs_location_id_idx ON public.location_sync_logs (location_id);
CREATE INDEX IF NOT EXISTS location_sync_logs_started_at_idx ON public.location_sync_logs (started_at DESC);
CREATE INDEX IF NOT EXISTS location_sync_logs_status_idx ON public.location_sync_logs (status);

ALTER TABLE public.location_sync_logs ENABLE ROW LEVEL SECURITY;
-- Service-role only — same convention as locations (023) and every
-- other admin-only table in this project.
