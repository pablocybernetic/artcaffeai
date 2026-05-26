-- ============================================================
-- Phase 2: AI Agents Schema Migration
-- Run in Supabase SQL editor (Dashboard → SQL Editor → New query)
-- ============================================================

-- 1. content_items — AI-generated ideas, drafts, and final copy
CREATE TABLE IF NOT EXISTS public.content_items (
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
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. agent_notifications — separate from any built-in Supabase notifications table
CREATE TABLE IF NOT EXISTS public.agent_notifications (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type       TEXT NOT NULL,
  payload    JSONB DEFAULT '{}',
  sent_at    TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Add agent columns to content_briefs
ALTER TABLE public.content_briefs
  ADD COLUMN IF NOT EXISTS research_summary  TEXT,
  ADD COLUMN IF NOT EXISTS market_data       JSONB,
  ADD COLUMN IF NOT EXISTS agent_brief       TEXT,
  ADD COLUMN IF NOT EXISTS ideation_job_id   UUID,
  ADD COLUMN IF NOT EXISTS production_job_id UUID;

-- 4. RLS
ALTER TABLE public.content_items       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_notifications ENABLE ROW LEVEL SECURITY;

-- Authenticated users can read and update content items
CREATE POLICY "auth_read_content_items"
  ON public.content_items FOR SELECT
  TO authenticated USING (true);

CREATE POLICY "auth_update_content_items"
  ON public.content_items FOR UPDATE
  TO authenticated USING (true);

-- Authenticated users can read all agent notifications
CREATE POLICY "auth_read_agent_notifications"
  ON public.agent_notifications FOR SELECT
  TO authenticated USING (true);

-- 5. Indexes
CREATE INDEX IF NOT EXISTS idx_content_items_brief_id
  ON public.content_items(brief_id);

CREATE INDEX IF NOT EXISTS idx_content_items_status
  ON public.content_items(status);

CREATE INDEX IF NOT EXISTS idx_agent_notifications_created_at
  ON public.agent_notifications(created_at DESC);
