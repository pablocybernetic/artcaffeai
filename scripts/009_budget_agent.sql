-- 009_budget_agent.sql
-- AI-generated budget analysis results, one row per run per concept.
-- Run in Supabase SQL editor before deploying the budget agent.

CREATE TABLE IF NOT EXISTS public.budget_recommendations (
  id              uuid                     NOT NULL DEFAULT uuid_generate_v4(),
  concept_id      uuid                     NOT NULL REFERENCES public.concepts(id) ON DELETE CASCADE,
  period_start    date,
  period_end      date,
  pacing_status   text                     NOT NULL DEFAULT 'on-track',  -- on-track | under | over | critical
  alert_level     text                     NOT NULL DEFAULT 'none',       -- none | info | warning | critical
  recommendations jsonb                    NOT NULL DEFAULT '[]'::jsonb,
  summary         text,
  model           text,
  created_at      timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT budget_recommendations_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS budget_recommendations_concept_created
  ON public.budget_recommendations (concept_id, created_at DESC);

-- Allow the service role (used by FastAPI) to read and write.
ALTER TABLE public.budget_recommendations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
  ON public.budget_recommendations
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

CREATE POLICY "Authenticated read"
  ON public.budget_recommendations
  FOR SELECT
  TO authenticated
  USING (true);
