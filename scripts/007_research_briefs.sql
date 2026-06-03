-- ============================================================
-- ARTCAFFE — Research Briefs Table
-- Stores structured opportunity analyses produced by the
-- Research Agent. One row per run per concept.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.research_briefs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_id    uuid NOT NULL REFERENCES public.concepts(id),
  period_start  date NOT NULL,
  period_end    date NOT NULL,
  opportunities jsonb NOT NULL DEFAULT '[]',
  summary       text,
  model         text,
  job_id        uuid REFERENCES public.jobs(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_research_briefs_concept
  ON public.research_briefs (concept_id, created_at DESC);

-- RLS
ALTER TABLE public.research_briefs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Authenticated users can read research briefs"
  ON public.research_briefs FOR SELECT TO authenticated
  USING (true);

CREATE POLICY "Service role can manage research briefs"
  ON public.research_briefs FOR ALL TO service_role
  USING (true) WITH CHECK (true);
