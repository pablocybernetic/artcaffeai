-- ============================================================
-- ARTCAFFE — Locations (Google Places sync source of truth)
--
-- MarketingAI + Supabase become the source of truth for Artcaffe and
-- Artcaffe Market store location data — Shopify no longer calls the
-- Google Places API directly (see fastAPI&backend/app/locations_public_routes.py).
--
-- Fields split into two ownership classes (never enforced by a DB
-- constraint, only by which code path writes them — see
-- google_location_sync_service.py):
--   GOOGLE-MANAGED:      rating, review_count, phone, international_phone,
--                        formatted_address, latitude/longitude,
--                        opening_hours, business-status-driven `status`
--                        transitions, google_photos, google_reviews,
--                        google_maps_url
--   MARKETINGAI-MANAGED: brand_type, store_code, descriptions, services,
--                        amenities, SEO fields, hero_image_url, shopify/
--                        menu/reservation/order URLs, nearby_landmarks,
--                        display_order, media/listing URLs below
-- `sync_from_google` lets an admin exempt a single location from the
-- cron entirely (spec: "Allow administrators to override Google-managed
-- values when necessary").
-- ============================================================

CREATE TABLE IF NOT EXISTS public.locations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  brand_type text NOT NULL CHECK (brand_type IN ('artcaffe_restaurant', 'artcaffe_market')),
  name text NOT NULL,
  slug text NOT NULL UNIQUE,
  store_code text,

  google_place_id text UNIQUE,
  google_business_location_id text,

  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'inactive', 'coming_soon', 'temporarily_closed', 'permanently_closed')),

  country text DEFAULT 'Kenya',
  county text,
  city text,
  area text,
  address text,
  formatted_address text,
  postal_code text,

  latitude double precision,
  longitude double precision,

  phone text,
  international_phone text,

  google_maps_url text,
  google_review_url text,

  website_url text,
  shopify_url text,
  menu_url text,
  reservation_url text,
  order_url text,

  short_description text,
  description text,

  primary_category text,
  secondary_categories jsonb NOT NULL DEFAULT '[]'::jsonb,

  opening_hours jsonb NOT NULL DEFAULT '{}'::jsonb,
  services jsonb NOT NULL DEFAULT '[]'::jsonb,
  amenities jsonb NOT NULL DEFAULT '[]'::jsonb,
  nearby_landmarks jsonb NOT NULL DEFAULT '[]'::jsonb,

  hero_image_url text,
  gallery jsonb NOT NULL DEFAULT '[]'::jsonb,

  rating numeric,
  review_count integer NOT NULL DEFAULT 0,
  google_reviews jsonb NOT NULL DEFAULT '[]'::jsonb,
  google_photos jsonb NOT NULL DEFAULT '[]'::jsonb,

  seo_title text,
  seo_description text,
  schema_type text NOT NULL DEFAULT 'Restaurant' CHECK (schema_type IN ('Restaurant', 'GroceryStore')),

  display_order integer NOT NULL DEFAULT 0,
  sync_from_google boolean NOT NULL DEFAULT true,

  last_google_sync_at timestamptz,
  last_google_sync_status text,
  last_google_sync_error text,
  google_raw_data jsonb,

  -- Media/listing links (spec §35) — MarketingAI-managed.
  google_business_profile_url text,
  tripadvisor_url text,
  facebook_url text,
  instagram_url text,
  tiktok_url text,
  uber_eats_url text,
  glovo_url text,
  media_mentions jsonb NOT NULL DEFAULT '[]'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS locations_brand_type_idx ON public.locations (brand_type);
CREATE INDEX IF NOT EXISTS locations_status_idx ON public.locations (status);
CREATE INDEX IF NOT EXISTS locations_city_idx ON public.locations (city);
CREATE INDEX IF NOT EXISTS locations_google_place_id_idx ON public.locations (google_place_id);

ALTER TABLE public.locations ENABLE ROW LEVEL SECURITY;
-- Service-role only (bypasses RLS entirely) — every read of this table,
-- public API included, goes through the FastAPI backend's service-role
-- client (see locations_public_routes.py), never directly from Shopify
-- or the browser, so no authenticated/anon policies are defined.
