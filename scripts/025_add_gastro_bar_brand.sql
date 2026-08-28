-- ============================================================
-- Add 'artcaffe_gastro_bar' as a third brand_type value.
--
-- Discovered via Artcaffe's live store-locator backend (rightchoice.ai)
-- that "Artcaffé Gastro Bar" is a distinct sub-brand (Westlands Square,
-- Imaara Mall) that doesn't fit either artcaffe_restaurant or
-- artcaffe_market. Urban Burgers / Artcaffé To Go are filed under
-- artcaffe_restaurant per user decision — no schema change needed for those.
-- ============================================================

ALTER TABLE public.locations DROP CONSTRAINT IF EXISTS locations_brand_type_check;

ALTER TABLE public.locations ADD CONSTRAINT locations_brand_type_check
  CHECK (brand_type IN ('artcaffe_restaurant', 'artcaffe_market', 'artcaffe_gastro_bar'));
