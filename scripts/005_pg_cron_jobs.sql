-- ============================================================
-- ARTCAFFE — Automated Scheduling via pg_cron
--
-- PREREQUISITES (do these first in Supabase Dashboard):
--   1. Database → Extensions → Enable "pg_cron"
--   2. Database → Extensions → Enable "pg_net"  (needed for HTTP calls)
--
-- All times are UTC. Artcaffe is UTC+3 (EAT).
-- For example: 3:00 UTC = 06:00 EAT.
--
-- To view scheduled jobs:   SELECT * FROM cron.job;
-- To view recent runs:      SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20;
-- To remove a job:          SELECT cron.unschedule('job-name');
-- ============================================================

-- ============================================================
-- 1. Stale job recovery  (pure SQL — no pg_net needed)
-- Jobs stuck in 'running' for more than 30 minutes are reset
-- back to 'pending' so the job_runner can retry them.
-- Runs every 15 minutes.
-- ============================================================
SELECT cron.schedule(
  'stale-job-recovery',
  '*/15 * * * *',
  $$
    UPDATE public.jobs
    SET
      status      = 'pending',
      started_at  = NULL,
      retry_count = retry_count + 1,
      updated_at  = now()
    WHERE
      status      = 'running'
      AND started_at < now() - INTERVAL '30 minutes'
      AND retry_count < max_retries;
  $$
);


-- ============================================================
-- 2. Old job cleanup  (pure SQL — no pg_net needed)
-- Prune succeeded/failed jobs older than 30 days to keep the
-- jobs table small.  Runs every Sunday at 02:00 UTC (05:00 EAT).
-- ============================================================
SELECT cron.schedule(
  'job-cleanup',
  '0 2 * * 0',
  $$
    DELETE FROM public.jobs
    WHERE
      status IN ('succeeded', 'failed')
      AND finished_at < now() - INTERVAL '30 days';
  $$
);


-- ============================================================
-- 3. Sent notification cleanup  (pure SQL — no pg_net needed)
-- Remove delivered notifications older than 90 days.
-- Runs every day at 04:00 UTC (07:00 EAT).
-- ============================================================
SELECT cron.schedule(
  'notification-cleanup',
  '0 4 * * *',
  $$
    DELETE FROM public.notifications
    WHERE
      sent_at IS NOT NULL
      AND created_at < now() - INTERVAL '90 days';
  $$
);


-- ============================================================
-- 4. Daily BigQuery snapshot  (requires pg_net + FastAPI)
-- Calls POST /data/snapshot on the FastAPI backend to pull
-- the latest GA4 + ads performance data into
-- platform_data_snapshots.  Runs at 03:00 UTC (06:00 EAT)
-- so fresh data is ready for the morning team.
--
-- ⚠ Replace 'YOUR_API_KEY' with the value of FASTAPI_API_KEY
--   from your server .env, or remove the header entirely if
--   API key auth is disabled.
-- ⚠ Replace 'http://127.0.0.1:8000' with the actual URL if
--   pg_cron does not run on the same host as FastAPI.
-- ============================================================
SELECT cron.schedule(
  'daily-snapshot',
  '0 3 * * *',
  $$
    SELECT net.http_post(
      url     := 'http://127.0.0.1:8000/data/snapshot',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'X-Api-Key',    'YOUR_API_KEY'
      ),
      body    := '{}'::jsonb
    )::text;
  $$
);


-- ============================================================
-- 5. Snapshot freshness check  (pure SQL — no pg_net needed)
-- Insert a monitoring row into agent_notifications if no
-- snapshot has arrived for any concept in the last 48 hours.
-- DBA can query agent_notifications to catch silent failures.
-- Runs every day at 08:00 UTC (11:00 EAT).
-- ============================================================
SELECT cron.schedule(
  'snapshot-freshness-check',
  '0 8 * * *',
  $$
    INSERT INTO public.agent_notifications (type, payload)
    SELECT
      'snapshot_stale',
      jsonb_build_object(
        'concept_id', c.id,
        'concept_key', c.key,
        'last_snapshot', MAX(s.snapshot_date),
        'checked_at', now()
      )
    FROM public.concepts c
    LEFT JOIN public.platform_data_snapshots s ON s.concept_id = c.id
    GROUP BY c.id, c.key
    HAVING MAX(s.snapshot_date) < CURRENT_DATE - 2
        OR MAX(s.snapshot_date) IS NULL;
  $$
);


-- ============================================================
-- OS-level cron fallback
-- If pg_cron or pg_net is unavailable, use the system crontab
-- on the VM instead.  SSH to 136.115.140.77 and run:
--   sudo crontab -e
-- Then add (adjust YOUR_API_KEY and path as needed):
--
--   # Daily BigQuery snapshot at 06:00 EAT (03:00 UTC)
--   0 3 * * * FASTAPI_API_KEY=YOUR_API_KEY /opt/artcaffe/scripts/schedule_snapshot.sh \
--             >> /var/log/artcaffe-snapshot.log 2>&1
-- ============================================================
