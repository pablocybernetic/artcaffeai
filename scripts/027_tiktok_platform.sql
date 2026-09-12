-- ============================================================
-- Add TikTok as a supported platform_credentials platform.
--
-- Also fixes a pre-existing latent bug found while adding this: the
-- original CHECK constraint only ever allowed ('meta','linkedin',
-- 'google_ads') even though whatsapp_publisher.py and twitter_publisher.py
-- were built and wired into publishing_routes.py long ago — saving
-- credentials for either would have failed at the DB layer with a
-- constraint violation. No row for either platform has ever been created
-- (verified live), so this has silently never worked. Fixing all three
-- gaps together since it costs nothing extra.
-- ============================================================

ALTER TABLE public.platform_credentials DROP CONSTRAINT IF EXISTS platform_credentials_platform_check;

ALTER TABLE public.platform_credentials ADD CONSTRAINT platform_credentials_platform_check
  CHECK (platform = ANY (ARRAY['meta'::text, 'linkedin'::text, 'google_ads'::text, 'twitter'::text, 'whatsapp'::text, 'tiktok'::text]));
