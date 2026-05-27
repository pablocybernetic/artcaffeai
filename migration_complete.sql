-- ============================================================
-- Artcaffe AI Marketing System — Complete Schema Migration
-- Safe to re-run: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS
-- Run in Supabase SQL Editor: Dashboard → SQL Editor → New query
--
-- Tables created / managed:
--   concepts, jobs, content_briefs, brand_contexts,
--   assets, platform_data_snapshots,
--   content_items, agent_notifications
-- ============================================================

-- ============================================================
-- 1. concepts
--    One row per brand concept (e.g. "Artcaffe Main", "Artcaffe Corporate")
-- ============================================================
CREATE TABLE IF NOT EXISTS public.concepts (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key        TEXT NOT NULL UNIQUE,          -- slug used in agent calls
  name       TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.concepts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_concepts" ON public.concepts;
CREATE POLICY "auth_read_concepts"
  ON public.concepts FOR SELECT TO authenticated USING (true);

-- ============================================================
-- 2. jobs
--    Tracks every background agent job (research / ideation / production)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.jobs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id    UUID REFERENCES public.concepts(id) ON DELETE SET NULL,
  agent_type    TEXT NOT NULL
                  CHECK (agent_type IN ('research', 'ideation', 'production')),
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
  input_payload JSONB DEFAULT '{}',
  result        JSONB,
  error_message TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW(),
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ
);

-- Add every column idempotently for pre-existing jobs tables
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS concept_id    UUID REFERENCES public.concepts(id) ON DELETE SET NULL;
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS agent_type    TEXT NOT NULL DEFAULT 'research';
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS status        TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS input_payload JSONB DEFAULT '{}';
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS result        JSONB;
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS started_at    TIMESTAMPTZ;
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS finished_at   TIMESTAMPTZ;

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_jobs" ON public.jobs;
CREATE POLICY "auth_read_jobs"
  ON public.jobs FOR SELECT TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_jobs_concept_id ON public.jobs(concept_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON public.jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_agent_type ON public.jobs(agent_type);

-- ============================================================
-- 3. content_briefs
--    A content brief is a creative request that moves through
--    the Kanban pipeline: ideation → review → approved → production → published
-- ============================================================
CREATE TABLE IF NOT EXISTS public.content_briefs (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id       UUID REFERENCES public.concepts(id) ON DELETE SET NULL,
  title            TEXT NOT NULL DEFAULT '',
  code             TEXT,
  platform         TEXT DEFAULT 'instagram',
  stage            TEXT NOT NULL DEFAULT 'ideation',
  content_status   TEXT DEFAULT 'draft',
  content_angle    TEXT,
  hook             TEXT,
  assignee         TEXT,
  comments         INT  NOT NULL DEFAULT 0,
  attachments      INT  NOT NULL DEFAULT 0,
  due_date         TIMESTAMPTZ,
  research_summary TEXT,
  market_data      JSONB,
  agent_brief      TEXT,
  ideation_job_id  UUID,
  production_job_id UUID,
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Add every column idempotently — covers tables that already exist with an older schema
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS concept_id        UUID REFERENCES public.concepts(id) ON DELETE SET NULL;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS code              TEXT;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS platform          TEXT DEFAULT 'instagram';
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS stage             TEXT NOT NULL DEFAULT 'ideation';
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS content_status    TEXT DEFAULT 'draft';
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS content_angle     TEXT;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS hook              TEXT;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS assignee          TEXT;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS comments          INT  NOT NULL DEFAULT 0;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS attachments       INT  NOT NULL DEFAULT 0;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS due_date          TIMESTAMPTZ;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS research_summary  TEXT;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS market_data       JSONB;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS agent_brief       TEXT;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS ideation_job_id   UUID;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS production_job_id UUID;
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS created_at        TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.content_briefs ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE public.content_briefs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_content_briefs"   ON public.content_briefs;
DROP POLICY IF EXISTS "auth_write_content_briefs"  ON public.content_briefs;
CREATE POLICY "auth_read_content_briefs"
  ON public.content_briefs FOR SELECT TO authenticated USING (true);
CREATE POLICY "auth_write_content_briefs"
  ON public.content_briefs FOR ALL TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_content_briefs_concept_id ON public.content_briefs(concept_id);
CREATE INDEX IF NOT EXISTS idx_content_briefs_stage      ON public.content_briefs(stage);

-- ============================================================
-- 4. brand_contexts
--    Structured JSON extracted from brand guideline PDFs.
--    One active row per concept; previous versions are kept for audit.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.brand_contexts (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id       UUID NOT NULL REFERENCES public.concepts(id) ON DELETE CASCADE,
  version          INT  NOT NULL DEFAULT 1,
  is_active        BOOLEAN NOT NULL DEFAULT TRUE,
  source_file_path TEXT,                          -- storage path of the source PDF
  context_json     JSONB NOT NULL DEFAULT '{}',   -- structured brand data
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.brand_contexts ADD COLUMN IF NOT EXISTS concept_id       UUID REFERENCES public.concepts(id) ON DELETE CASCADE;
ALTER TABLE public.brand_contexts ADD COLUMN IF NOT EXISTS version          INT  NOT NULL DEFAULT 1;
ALTER TABLE public.brand_contexts ADD COLUMN IF NOT EXISTS is_active        BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.brand_contexts ADD COLUMN IF NOT EXISTS source_file_path TEXT;
ALTER TABLE public.brand_contexts ADD COLUMN IF NOT EXISTS context_json     JSONB NOT NULL DEFAULT '{}';
ALTER TABLE public.brand_contexts ADD COLUMN IF NOT EXISTS created_at       TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE public.brand_contexts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_brand_contexts" ON public.brand_contexts;
CREATE POLICY "auth_read_brand_contexts"
  ON public.brand_contexts FOR SELECT TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_brand_contexts_concept_active
  ON public.brand_contexts(concept_id, is_active);

-- ============================================================
-- 5. assets
--    Images and videos uploaded to Supabase Storage and referenced
--    by content_items. Agents pick asset UUIDs when generating ideas.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.assets (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id  UUID REFERENCES public.concepts(id) ON DELETE SET NULL,
  filename    TEXT NOT NULL,
  asset_type  TEXT NOT NULL DEFAULT 'image'
                CHECK (asset_type IN ('image', 'video', 'document')),
  platform    TEXT,                               -- optional: instagram, facebook…
  public_url  TEXT,                               -- Supabase Storage public URL
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS concept_id  UUID REFERENCES public.concepts(id) ON DELETE SET NULL;
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS filename    TEXT NOT NULL DEFAULT '';
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS asset_type  TEXT NOT NULL DEFAULT 'image';
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS platform    TEXT;
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS public_url  TEXT;
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_assets"  ON public.assets;
DROP POLICY IF EXISTS "auth_write_assets" ON public.assets;
CREATE POLICY "auth_read_assets"
  ON public.assets FOR SELECT TO authenticated USING (true);
CREATE POLICY "auth_write_assets"
  ON public.assets FOR ALL TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_assets_concept_id  ON public.assets(concept_id);
CREATE INDEX IF NOT EXISTS idx_assets_asset_type  ON public.assets(asset_type);

-- ============================================================
-- 6. platform_data_snapshots
--    Weekly BigQuery pull results (GA4 + Google Ads).
--    The Data Agent reads these to answer analytics questions.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.platform_data_snapshots (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id    UUID REFERENCES public.concepts(id) ON DELETE SET NULL,
  platform      TEXT NOT NULL,                    -- 'ga4' | 'paid_ads'
  snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE,
  summary_json  JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (concept_id, platform, snapshot_date)
);

ALTER TABLE public.platform_data_snapshots ADD COLUMN IF NOT EXISTS concept_id    UUID REFERENCES public.concepts(id) ON DELETE SET NULL;
ALTER TABLE public.platform_data_snapshots ADD COLUMN IF NOT EXISTS platform      TEXT NOT NULL DEFAULT 'ga4';
ALTER TABLE public.platform_data_snapshots ADD COLUMN IF NOT EXISTS snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE public.platform_data_snapshots ADD COLUMN IF NOT EXISTS summary_json  JSONB NOT NULL DEFAULT '{}';
ALTER TABLE public.platform_data_snapshots ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.platform_data_snapshots ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE public.platform_data_snapshots ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_snapshots" ON public.platform_data_snapshots;
CREATE POLICY "auth_read_snapshots"
  ON public.platform_data_snapshots FOR SELECT TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_snapshots_concept_date
  ON public.platform_data_snapshots(concept_id, snapshot_date DESC);

-- ============================================================
-- 7. content_items
--    AI-generated concepts and final copy produced by the agents.
--    asset_ids references rows in the assets table.
-- ============================================================
DROP TABLE IF EXISTS public.content_items CASCADE;

CREATE TABLE public.content_items (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  brief_id   UUID NOT NULL REFERENCES public.content_briefs(id) ON DELETE CASCADE,
  job_id     UUID,
  type       TEXT NOT NULL DEFAULT 'concept'
               CHECK (type IN ('concept', 'draft', 'final')),
  platform   TEXT,
  title      TEXT,
  headline   TEXT,
  caption    TEXT,
  body       TEXT,
  channels   TEXT[],
  rationale  TEXT,
  metadata   JSONB DEFAULT '{}',
  version    INT  NOT NULL DEFAULT 1,
  status     TEXT NOT NULL DEFAULT 'draft'
               CHECK (status IN ('draft', 'pending_review', 'approved', 'rejected')),
  asset_ids  UUID[] DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.content_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_content_items"   ON public.content_items;
DROP POLICY IF EXISTS "auth_update_content_items" ON public.content_items;
CREATE POLICY "auth_read_content_items"
  ON public.content_items FOR SELECT TO authenticated USING (true);
CREATE POLICY "auth_update_content_items"
  ON public.content_items FOR UPDATE TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_content_items_brief_id ON public.content_items(brief_id);
CREATE INDEX IF NOT EXISTS idx_content_items_status   ON public.content_items(status);

-- ============================================================
-- 8. agent_notifications
--    In-app notifications sent by agents on job completion.
--    Named agent_notifications to avoid conflict with Supabase's
--    built-in notifications table.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.agent_notifications (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type       TEXT NOT NULL,
  payload    JSONB DEFAULT '{}',
  sent_at    TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.agent_notifications ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_agent_notifications" ON public.agent_notifications;
CREATE POLICY "auth_read_agent_notifications"
  ON public.agent_notifications FOR SELECT TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_agent_notifications_created_at
  ON public.agent_notifications(created_at DESC);

-- ============================================================
-- Done.
-- ============================================================
