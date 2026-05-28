-- ============================================================
-- Publishing infrastructure
-- Safe to re-run: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS
-- Run in Supabase SQL Editor before deploying publishing feature.
-- ============================================================

-- platform_credentials
-- Stores API credentials for each social platform (one row per platform).
-- Tokens stored server-side only; frontend never reads raw token values.
CREATE TABLE IF NOT EXISTS public.platform_credentials (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  platform        TEXT NOT NULL UNIQUE
                    CHECK (platform IN ('meta', 'linkedin', 'google_ads')),
  -- shared
  access_token    TEXT,
  account_name    TEXT,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  -- meta (instagram + facebook)
  page_id         TEXT,
  ig_user_id      TEXT,
  -- linkedin
  org_id          TEXT,
  -- google ads
  developer_token TEXT,
  customer_id     TEXT,
  campaign_id     TEXT,
  ad_group_id     TEXT,
  final_url       TEXT,
  -- audit
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Add columns idempotently for pre-existing tables
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS access_token    TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS account_name    TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS is_active       BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS page_id         TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS ig_user_id      TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS org_id          TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS developer_token TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS customer_id     TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS campaign_id     TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS ad_group_id     TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS final_url       TEXT;
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS created_at      TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMPTZ DEFAULT NOW();

-- RLS: authenticated users can read credential status (not token values — those stay in FastAPI)
ALTER TABLE public.platform_credentials ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_platform_credentials" ON public.platform_credentials;
CREATE POLICY "auth_read_platform_credentials"
  ON public.platform_credentials FOR SELECT TO authenticated USING (true);

-- published_posts
-- Audit trail of every publish attempt (success or failure) per content item.
CREATE TABLE IF NOT EXISTS public.published_posts (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content_item_id    UUID REFERENCES public.content_items(id) ON DELETE SET NULL,
  concept_id         UUID REFERENCES public.concepts(id) ON DELETE SET NULL,
  platform           TEXT NOT NULL,
  platform_post_id   TEXT,
  platform_post_url  TEXT,
  status             TEXT NOT NULL DEFAULT 'published'
                       CHECK (status IN ('published', 'failed', 'scheduled')),
  error_message      TEXT,
  published_at       TIMESTAMPTZ DEFAULT NOW(),
  created_at         TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS content_item_id   UUID REFERENCES public.content_items(id) ON DELETE SET NULL;
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS concept_id        UUID REFERENCES public.concepts(id) ON DELETE SET NULL;
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS platform          TEXT;
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS platform_post_id  TEXT;
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS platform_post_url TEXT;
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS status            TEXT NOT NULL DEFAULT 'published';
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS error_message     TEXT;
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS published_at      TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.published_posts ADD COLUMN IF NOT EXISTS created_at        TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE public.published_posts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "auth_read_published_posts"  ON public.published_posts;
DROP POLICY IF EXISTS "auth_write_published_posts" ON public.published_posts;
CREATE POLICY "auth_read_published_posts"
  ON public.published_posts FOR SELECT TO authenticated USING (true);
CREATE POLICY "auth_write_published_posts"
  ON public.published_posts FOR ALL TO authenticated USING (true);

CREATE INDEX IF NOT EXISTS idx_published_posts_item_id ON public.published_posts(content_item_id);
CREATE INDEX IF NOT EXISTS idx_published_posts_platform ON public.published_posts(platform);

-- ============================================================
-- Done. Re-run safe.
-- ============================================================
