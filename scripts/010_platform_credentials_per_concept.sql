-- ============================================================
-- ARTCAFFE — Per-brand Meta (Instagram/Facebook) credentials
-- Run in the Supabase SQL editor.
--
-- Lets each concept (market / restaurant / gastro_bar) connect its own
-- Facebook Page + Instagram Business account. concept_id is NULL for
-- platforms that stay global (linkedin, google_ads, twitter, whatsapp).
--
-- After running this, the existing single "meta" row keeps concept_id
-- NULL and is effectively orphaned — reconnect Meta credentials for
-- each of the three brands from Settings → Integrations.
-- ============================================================

ALTER TABLE public.platform_credentials
  ADD COLUMN IF NOT EXISTS concept_id uuid REFERENCES public.concepts(id);

ALTER TABLE public.platform_credentials
  DROP CONSTRAINT IF EXISTS platform_credentials_platform_key;

ALTER TABLE public.platform_credentials
  ADD CONSTRAINT platform_credentials_platform_concept_key UNIQUE (platform, concept_id);
