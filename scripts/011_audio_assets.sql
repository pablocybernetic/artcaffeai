-- ============================================================
-- ARTCAFFE — Audio assets (background music for videos)
-- Run in the Supabase SQL editor.
--
-- Lets background-music tracks be stored as first-class assets
-- (asset_type='audio'), reusable across posts. Muxed into video
-- files server-side before publish — Meta's API has no way to
-- attach IG/FB's licensed music catalog to a post directly.
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum
    WHERE enumlabel = 'audio'
      AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'asset_type')
  ) THEN
    ALTER TYPE asset_type ADD VALUE 'audio';
  END IF;
END;
$$;
