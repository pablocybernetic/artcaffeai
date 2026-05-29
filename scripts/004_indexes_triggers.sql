-- ============================================================
-- ARTCAFFE — Performance Indexes + updated_at Auto-Triggers
-- Run in the Supabase SQL editor after 003_rls_policies.sql.
-- All CREATE INDEX statements are IF NOT EXISTS — safe to re-run.
-- ============================================================


-- ============================================================
-- 1. updated_at auto-trigger
-- Keeps updated_at in sync on any UPDATE without requiring
-- the caller to manually set it.
-- ============================================================

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- Apply to every table that has an updated_at column.
-- DROP ... IF EXISTS first so this file is idempotent on re-run.

DO $$
DECLARE
  tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'content_briefs',
    'content_items',
    'assets',
    'calendar_entries',
    'brand_contexts',
    'budget_allocations',
    'jobs',
    'research_briefs',
    'team_members',
    'concepts',
    'platform_credentials',
    'feedback_comments',
    'platform_data_snapshots'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON public.%1$s;
       CREATE TRIGGER trg_%1$s_updated_at
         BEFORE UPDATE ON public.%1$s
         FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();',
      tbl
    );
  END LOOP;
END;
$$;


-- ============================================================
-- 2. Performance indexes
-- Each group targets a common query pattern from the frontend
-- or the FastAPI job runner.
-- ============================================================

-- --- content_briefs ---
-- Kanban: filtered by concept + status, ordered by created_at
CREATE INDEX IF NOT EXISTS idx_briefs_concept_id
  ON public.content_briefs (concept_id);

CREATE INDEX IF NOT EXISTS idx_briefs_content_status
  ON public.content_briefs (content_status);

CREATE INDEX IF NOT EXISTS idx_briefs_stage
  ON public.content_briefs (stage);

CREATE INDEX IF NOT EXISTS idx_briefs_created_at
  ON public.content_briefs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_briefs_concept_status
  ON public.content_briefs (concept_id, content_status);

-- --- content_items ---
-- Ideation panel: all items for a brief, ordered by created_at
CREATE INDEX IF NOT EXISTS idx_items_brief_id
  ON public.content_items (brief_id);

CREATE INDEX IF NOT EXISTS idx_items_status
  ON public.content_items (status);

CREATE INDEX IF NOT EXISTS idx_items_brief_status
  ON public.content_items (brief_id, status);

CREATE INDEX IF NOT EXISTS idx_items_created_at
  ON public.content_items (created_at DESC);

-- --- assets ---
-- Asset library: filtered by concept + approval_status
CREATE INDEX IF NOT EXISTS idx_assets_concept_id
  ON public.assets (concept_id);

CREATE INDEX IF NOT EXISTS idx_assets_brief_id
  ON public.assets (brief_id);

CREATE INDEX IF NOT EXISTS idx_assets_approval_status
  ON public.assets (approval_status);

CREATE INDEX IF NOT EXISTS idx_assets_concept_approved
  ON public.assets (concept_id, approval_status);

CREATE INDEX IF NOT EXISTS idx_assets_created_at
  ON public.assets (created_at DESC);

-- --- calendar_entries ---
-- Calendar view: filtered by concept, ordered by scheduled_at
CREATE INDEX IF NOT EXISTS idx_calendar_concept_id
  ON public.calendar_entries (concept_id);

CREATE INDEX IF NOT EXISTS idx_calendar_scheduled_at
  ON public.calendar_entries (scheduled_at);

CREATE INDEX IF NOT EXISTS idx_calendar_concept_date
  ON public.calendar_entries (concept_id, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_calendar_content_status
  ON public.calendar_entries (content_status);

-- --- platform_data_snapshots ---
-- Data agent: recent snapshots per concept + platform
CREATE INDEX IF NOT EXISTS idx_snapshots_concept_platform
  ON public.platform_data_snapshots (concept_id, platform);

CREATE INDEX IF NOT EXISTS idx_snapshots_date_desc
  ON public.platform_data_snapshots (snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_concept_date
  ON public.platform_data_snapshots (concept_id, snapshot_date DESC);

-- Unique constraint so upsert (on_conflict) works correctly
CREATE UNIQUE INDEX IF NOT EXISTS uidx_snapshots_concept_platform_date
  ON public.platform_data_snapshots (concept_id, platform, snapshot_date);

-- --- brand_contexts ---
-- Settings brand tab: latest active context per concept
CREATE INDEX IF NOT EXISTS idx_brand_ctx_concept_active
  ON public.brand_contexts (concept_id, is_active);

CREATE INDEX IF NOT EXISTS idx_brand_ctx_concept_version
  ON public.brand_contexts (concept_id, version DESC);

-- --- jobs ---
-- Job runner poll: pending jobs ordered by queue time
CREATE INDEX IF NOT EXISTS idx_jobs_status
  ON public.jobs (status);

CREATE INDEX IF NOT EXISTS idx_jobs_concept_id
  ON public.jobs (concept_id);

CREATE INDEX IF NOT EXISTS idx_jobs_agent_type
  ON public.jobs (agent_type);

CREATE INDEX IF NOT EXISTS idx_jobs_pending_queue
  ON public.jobs (queued_at ASC)
  WHERE status = 'pending';

-- Stale job recovery: running jobs older than X minutes
CREATE INDEX IF NOT EXISTS idx_jobs_running_started
  ON public.jobs (started_at)
  WHERE status = 'running';

-- Cleanup: old completed jobs
CREATE INDEX IF NOT EXISTS idx_jobs_finished_at
  ON public.jobs (finished_at DESC)
  WHERE status IN ('succeeded', 'failed');

-- --- research_briefs ---
CREATE INDEX IF NOT EXISTS idx_research_concept_id
  ON public.research_briefs (concept_id);

CREATE INDEX IF NOT EXISTS idx_research_week_start
  ON public.research_briefs (week_start DESC);

-- --- notifications ---
-- Notification bell: unread notifications per recipient
CREATE INDEX IF NOT EXISTS idx_notifications_recipient
  ON public.notifications (recipient_id);

CREATE INDEX IF NOT EXISTS idx_notifications_unsent
  ON public.notifications (created_at DESC)
  WHERE sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notifications_recipient_unsent
  ON public.notifications (recipient_id, created_at DESC)
  WHERE sent_at IS NULL;

-- --- feedback_comments ---
CREATE INDEX IF NOT EXISTS idx_comments_brief_id
  ON public.feedback_comments (brief_id);

CREATE INDEX IF NOT EXISTS idx_comments_content_item
  ON public.feedback_comments (content_item_id);

CREATE INDEX IF NOT EXISTS idx_comments_author_id
  ON public.feedback_comments (author_id);

-- --- approval_events ---
CREATE INDEX IF NOT EXISTS idx_approvals_brief_id
  ON public.approval_events (brief_id);

CREATE INDEX IF NOT EXISTS idx_approvals_reviewer_id
  ON public.approval_events (reviewer_id);

CREATE INDEX IF NOT EXISTS idx_approvals_created_at
  ON public.approval_events (created_at DESC);

-- --- published_posts ---
CREATE INDEX IF NOT EXISTS idx_published_concept_id
  ON public.published_posts (concept_id);

CREATE INDEX IF NOT EXISTS idx_published_status
  ON public.published_posts (status);

CREATE INDEX IF NOT EXISTS idx_published_at
  ON public.published_posts (published_at DESC);

-- --- member_concept_access ---
CREATE INDEX IF NOT EXISTS idx_mca_member_id
  ON public.member_concept_access (member_id);

CREATE INDEX IF NOT EXISTS idx_mca_concept_id
  ON public.member_concept_access (concept_id);


-- ============================================================
-- 3. Realtime — enable on tables the UI subscribes to
-- ============================================================
DO $$
DECLARE
  tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'jobs',
    'content_items',
    'content_briefs',
    'calendar_entries',
    'notifications',
    'brand_contexts'
  ]
  LOOP
    BEGIN
      EXECUTE format(
        'ALTER PUBLICATION supabase_realtime ADD TABLE public.%I;', tbl
      );
    EXCEPTION WHEN duplicate_object THEN
      NULL; -- already in publication, skip
    END;
  END LOOP;
END;
$$;
