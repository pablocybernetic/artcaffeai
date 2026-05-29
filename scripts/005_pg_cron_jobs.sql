-- ============================================================
-- ARTCAFFE — Automated Scheduling
-- This script is safe to run even if pg_cron is not enabled.
-- Jobs that require pg_cron will print a NOTICE and be skipped.
--
-- To fully activate all jobs:
--   Supabase Dashboard → Database → Extensions → Enable pg_cron
--   Supabase Dashboard → Database → Extensions → Enable pg_net
--   Then re-run this script.
--
-- All times are UTC. Artcaffe is UTC+3 (EAT).
-- ============================================================

DO $$
DECLARE
  has_cron boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron');
  has_net  boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_net');
BEGIN

  -- ──────────────────────────────────────────────────────────
  -- 1. Stale job recovery  (requires pg_cron)
  -- Resets jobs stuck in 'running' for > 30 minutes back to
  -- 'pending' so the job_runner can retry them.
  -- Runs every 15 minutes.
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    PERFORM cron.unschedule('stale-job-recovery');
  EXCEPTION WHEN others THEN NULL;
  END IF;

  IF has_cron THEN
    PERFORM cron.schedule(
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
          status     = 'running'
          AND started_at < now() - INTERVAL '30 minutes'
          AND retry_count < max_retries;
      $$
    );
    RAISE NOTICE 'Scheduled: stale-job-recovery (every 15 min)';
  ELSE
    RAISE NOTICE 'SKIPPED stale-job-recovery — enable pg_cron extension first';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 2. Old job cleanup  (requires pg_cron)
  -- Deletes succeeded/failed jobs older than 30 days.
  -- Runs every Sunday at 02:00 UTC (05:00 EAT).
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('job-cleanup'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'job-cleanup',
      '0 2 * * 0',
      $$
        DELETE FROM public.jobs
        WHERE
          status IN ('succeeded', 'failed')
          AND finished_at < now() - INTERVAL '30 days';
      $$
    );
    RAISE NOTICE 'Scheduled: job-cleanup (weekly Sunday 02:00 UTC)';
  ELSE
    RAISE NOTICE 'SKIPPED job-cleanup — enable pg_cron extension first';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 3. Sent notification cleanup  (requires pg_cron)
  -- Removes delivered notifications older than 90 days.
  -- Runs daily at 04:00 UTC (07:00 EAT).
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('notification-cleanup'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'notification-cleanup',
      '0 4 * * *',
      $$
        DELETE FROM public.notifications
        WHERE
          sent_at IS NOT NULL
          AND created_at < now() - INTERVAL '90 days';
      $$
    );
    RAISE NOTICE 'Scheduled: notification-cleanup (daily 04:00 UTC)';
  ELSE
    RAISE NOTICE 'SKIPPED notification-cleanup — enable pg_cron extension first';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 4. Daily BigQuery snapshot  (requires pg_cron + pg_net)
  -- Calls POST /data/snapshot on FastAPI at 03:00 UTC (06:00 EAT).
  --
  -- ⚠ Replace YOUR_API_KEY with the FASTAPI_API_KEY from .env
  --   or remove the X-Api-Key header if auth is disabled.
  -- ⚠ Replace 127.0.0.1:8000 if pg_cron runs on a different host.
  -- ──────────────────────────────────────────────────────────
  IF has_cron AND has_net THEN
    BEGIN PERFORM cron.unschedule('daily-snapshot'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
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
    RAISE NOTICE 'Scheduled: daily-snapshot (daily 03:00 UTC via pg_net)';
  ELSIF has_cron AND NOT has_net THEN
    RAISE NOTICE 'SKIPPED daily-snapshot — enable pg_net extension, then re-run';
    RAISE NOTICE 'Alternative: add OS cron job using scripts/schedule_snapshot.sh';
  ELSE
    RAISE NOTICE 'SKIPPED daily-snapshot — enable pg_cron and pg_net extensions first';
    RAISE NOTICE 'Alternative: add OS cron job using scripts/schedule_snapshot.sh';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 5. Snapshot freshness check  (requires pg_cron, no pg_net)
  -- Writes to agent_notifications if any concept snapshot is
  -- older than 48 hours — silent failure detection.
  -- Runs daily at 08:00 UTC (11:00 EAT).
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('snapshot-freshness-check'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'snapshot-freshness-check',
      '0 8 * * *',
      $$
        INSERT INTO public.agent_notifications (type, payload)
        SELECT
          'snapshot_stale',
          jsonb_build_object(
            'concept_id',    c.id,
            'concept_key',   c.key,
            'last_snapshot', MAX(s.snapshot_date),
            'checked_at',    now()
          )
        FROM public.concepts c
        LEFT JOIN public.platform_data_snapshots s ON s.concept_id = c.id
        GROUP BY c.id, c.key
        HAVING MAX(s.snapshot_date) < CURRENT_DATE - 2
            OR MAX(s.snapshot_date) IS NULL;
      $$
    );
    RAISE NOTICE 'Scheduled: snapshot-freshness-check (daily 08:00 UTC)';
  ELSE
    RAISE NOTICE 'SKIPPED snapshot-freshness-check — enable pg_cron extension first';
  END IF;

END;
$$;

-- ============================================================
-- View scheduled jobs (run after enabling pg_cron):
--   SELECT jobname, schedule, command FROM cron.job ORDER BY jobname;
-- View recent runs:
--   SELECT jobname, status, start_time, end_time, return_message
--   FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20;
-- Remove a job:
--   SELECT cron.unschedule('job-name');
-- ============================================================
