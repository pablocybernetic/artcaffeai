-- ============================================================
-- Assets table — add missing columns for file metadata + AI analysis
-- Safe to re-run: uses ADD COLUMN IF NOT EXISTS
-- Run in Supabase SQL Editor before deploying image analysis feature.
-- ============================================================

-- Columns inserted by the upload dialog but absent from original migration
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS storage_path    TEXT;
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS mime_type       TEXT;
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT;
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMPTZ DEFAULT NOW();

-- AI vision analysis output (written by image_analysis_agent.py)
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS metadata         JSONB DEFAULT '{}';
ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS analysis_status  TEXT  DEFAULT 'pending';
-- analysis_status values: pending | done | failed | skipped

-- Speed up ideation asset queries (filter analysed images)
CREATE INDEX IF NOT EXISTS idx_assets_analysis_status
  ON public.assets(analysis_status);

-- ============================================================
-- Done. Re-run safe.
-- ============================================================
