-- ============================================================
-- ARTCAFFE — Multi-brand asset tagging
-- Run in the Supabase SQL editor.
--
-- assets.concept_id stays the single "brand this was made for" —
-- unchanged, still used by generation/briefs/publishing everywhere.
-- This adds a supplementary tags table controlling which brands'
-- Asset Library views show a given asset. A trigger auto-seeds the
-- primary concept as a tag, so every asset is visible under its own
-- brand by default with no other code changes needed — only
-- *additional* tagging (e.g. a shared stock photo) is new behavior.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.asset_concept_tags (
  asset_id uuid NOT NULL REFERENCES public.assets(id) ON DELETE CASCADE,
  concept_id uuid NOT NULL REFERENCES public.concepts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT asset_concept_tags_pkey PRIMARY KEY (asset_id, concept_id)
);

-- Backfill: every existing asset is tagged (at least) to its own primary concept
INSERT INTO public.asset_concept_tags (asset_id, concept_id)
SELECT id, concept_id FROM public.assets
ON CONFLICT DO NOTHING;

-- Auto-seed the primary concept tag on every future asset insert
CREATE OR REPLACE FUNCTION public.seed_asset_concept_tag()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO public.asset_concept_tags (asset_id, concept_id)
  VALUES (NEW.id, NEW.concept_id)
  ON CONFLICT DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_seed_asset_concept_tag ON public.assets;
CREATE TRIGGER trg_seed_asset_concept_tag
AFTER INSERT ON public.assets
FOR EACH ROW EXECUTE FUNCTION public.seed_asset_concept_tag();

-- RLS — mirrors the assets_* / mca_* policy shapes already in 003_rls_policies.sql
ALTER TABLE public.asset_concept_tags ENABLE ROW LEVEL SECURITY;

CREATE POLICY "asset_concept_tags_select" ON public.asset_concept_tags
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "asset_concept_tags_insert" ON public.asset_concept_tags
  FOR INSERT TO authenticated
  WITH CHECK (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
  );

CREATE POLICY "asset_concept_tags_delete" ON public.asset_concept_tags
  FOR DELETE TO authenticated
  USING (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
  );

-- Widen assets_select so brand-tagged (not just primary-concept) assets are
-- actually readable by someone whose access is only to the tagged brand.
DROP POLICY IF EXISTS "assets_select" ON public.assets;
CREATE POLICY "assets_select" ON public.assets
  FOR SELECT TO authenticated
  USING (
    public.is_admin()
    OR concept_id = ANY(public.my_concept_ids())
    OR EXISTS (
      SELECT 1 FROM public.asset_concept_tags t
      WHERE t.asset_id = assets.id AND t.concept_id = ANY(public.my_concept_ids())
    )
  );
