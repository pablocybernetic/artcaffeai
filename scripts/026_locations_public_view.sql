-- ============================================================
-- Public read-only view over `locations`, for Shopify to query
-- directly via PostgREST (https://supabase.artcaffe.co.ke/rest/v1)
-- using the public anon key, instead of routing through FastAPI.
--
-- `locations` itself keeps RLS enabled with zero policies (service-role
-- only, same convention as every other table here) -- this view is the
-- one deliberate public surface, scoped to:
--   - active locations only (no draft/inactive/closed rows)
--   - a public-safe column list only (mirrors locations_public_routes.py's
--     _serialize_summary/_serialize_detail -- excludes google_raw_data,
--     which is a large internal blob, not secret but not meant for public
--     consumption either)
-- ============================================================

CREATE OR REPLACE VIEW public.locations_public AS
SELECT
  id, brand_type, name, slug, store_code, status,
  country, county, city, area, address, formatted_address, postal_code,
  latitude, longitude,
  phone, international_phone,
  google_place_id, google_maps_url, google_review_url,
  website_url, shopify_url, menu_url, reservation_url, order_url,
  short_description, description,
  primary_category, secondary_categories,
  opening_hours, services, amenities, nearby_landmarks,
  hero_image_url, gallery,
  rating, review_count, google_reviews, google_photos,
  seo_title, seo_description, schema_type,
  display_order, last_google_sync_at
FROM public.locations
WHERE status = 'active';

GRANT SELECT ON public.locations_public TO anon, authenticated;
