-- ============================================================
-- ARTCAFFE — Automated Scheduling
-- Safe to run even if pg_cron is not enabled — jobs are skipped
-- with a NOTICE instead of erroring.
--
-- To fully activate all jobs:
--   Supabase Dashboard → Database → Extensions → Enable pg_cron
--   Supabase Dashboard → Database → Extensions → Enable pg_net
--   Then re-run this script.
--
-- All times are UTC. Artcaffe is UTC+3 (EAT).
-- ============================================================

-- Note: inner job SQL strings use $job$ tags (not $$) to avoid
-- conflicting with the outer DO $$ block delimiter.

DO $outer$
DECLARE
  has_cron boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron');
  has_net  boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_net');
BEGIN

  -- ──────────────────────────────────────────────────────────
  -- 1. Stale job recovery  (pg_cron only)
  -- Resets jobs stuck in 'running' for > 30 min → 'pending'.
  -- Runs every 15 minutes.
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('stale-job-recovery'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'stale-job-recovery',
      '*/15 * * * *',
      $job$
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
      $job$
    );
    RAISE NOTICE 'Scheduled: stale-job-recovery (every 15 min)';
  ELSE
    RAISE NOTICE 'SKIPPED stale-job-recovery — enable pg_cron in Dashboard → Extensions';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 2. Old job cleanup  (pg_cron only)
  -- Deletes succeeded/failed jobs older than 30 days.
  -- Runs every Sunday at 02:00 UTC (05:00 EAT).
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('job-cleanup'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'job-cleanup',
      '0 2 * * 0',
      $job$
        DELETE FROM public.jobs
        WHERE
          status IN ('succeeded', 'failed')
          AND finished_at < now() - INTERVAL '30 days';
      $job$
    );
    RAISE NOTICE 'Scheduled: job-cleanup (weekly Sunday 02:00 UTC)';
  ELSE
    RAISE NOTICE 'SKIPPED job-cleanup — enable pg_cron in Dashboard → Extensions';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 3. Sent notification cleanup  (pg_cron only)
  -- Removes delivered notifications older than 90 days.
  -- Runs daily at 04:00 UTC (07:00 EAT).
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('notification-cleanup'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'notification-cleanup',
      '0 4 * * *',
      $job$
        DELETE FROM public.notifications
        WHERE
          sent_at IS NOT NULL
          AND created_at < now() - INTERVAL '90 days';
      $job$
    );
    RAISE NOTICE 'Scheduled: notification-cleanup (daily 04:00 UTC)';
  ELSE
    RAISE NOTICE 'SKIPPED notification-cleanup — enable pg_cron in Dashboard → Extensions';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 4. Daily BigQuery snapshot  (pg_cron + pg_net)
  -- Calls POST /data/snapshot at 03:00 UTC (06:00 EAT).
  --
  -- ⚠ Replace YOUR_API_KEY with the value of FASTAPI_API_KEY
  --   from your server .env, or remove the header if auth is off.
  -- ──────────────────────────────────────────────────────────
  IF has_cron AND has_net THEN
    BEGIN PERFORM cron.unschedule('daily-snapshot'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'daily-snapshot',
      '0 3 * * *',
      $job$
        SELECT net.http_post(
          url     := 'http://127.0.0.1:8000/data/snapshot',
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Api-Key',    'YOUR_API_KEY'
          ),
          body    := '{}'::jsonb
        )::text;
      $job$
    );
    RAISE NOTICE 'Scheduled: daily-snapshot (daily 03:00 UTC via pg_net)';
  ELSIF has_cron THEN
    RAISE NOTICE 'SKIPPED daily-snapshot HTTP call — enable pg_net in Dashboard → Extensions';
    RAISE NOTICE 'Alternative: use scripts/schedule_snapshot.sh as an OS cron job on the VM';
  ELSE
    RAISE NOTICE 'SKIPPED daily-snapshot — enable pg_cron and pg_net in Dashboard → Extensions';
  END IF;

  -- ──────────────────────────────────────────────────────────
  -- 5. Snapshot freshness check  (pg_cron only, no pg_net)
  -- Writes to agent_notifications if any concept has no snapshot
  -- in the last 48 hours — silent failure detection.
  -- Runs daily at 08:00 UTC (11:00 EAT).
  -- ──────────────────────────────────────────────────────────
  IF has_cron THEN
    BEGIN PERFORM cron.unschedule('snapshot-freshness-check'); EXCEPTION WHEN others THEN NULL; END;
    PERFORM cron.schedule(
      'snapshot-freshness-check',
      '0 8 * * *',
      $job$
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
      $job$
    );
    RAISE NOTICE 'Scheduled: snapshot-freshness-check (daily 08:00 UTC)';
  ELSE
    RAISE NOTICE 'SKIPPED snapshot-freshness-check — enable pg_cron in Dashboard → Extensions';
  END IF;

END;
$outer$;

-- ============================================================
-- After enabling pg_cron, view results with:
--   SELECT jobname, schedule FROM cron.job ORDER BY jobname;
--   SELECT jobname, status, start_time, return_message
--   FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20;
-- ============================================================
