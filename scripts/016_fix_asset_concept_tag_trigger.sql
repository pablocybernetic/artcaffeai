-- ============================================================
-- ARTCAFFE — Fix seed_asset_concept_tag() trigger blocking client uploads
--
-- seed_asset_concept_tag() (012_asset_concept_tags.sql) runs AFTER INSERT
-- on assets and writes a derived, system-maintained row into
-- asset_concept_tags (asset_id, concept_id) — always the SAME concept_id
-- the assets row itself was just inserted with. Because the function
-- wasn't SECURITY DEFINER, that write ran as the calling (authenticated)
-- user and had to independently pass asset_concept_tags_insert's RLS
-- check. Any direct client-side insert into assets (e.g. uploading a
-- photo from the browser instead of via the service-role backend) could
-- pass assets_insert's own check yet still have this trigger's derived
-- insert rejected, rolling back the whole assets row with a confusing
-- "new row violates row-level security policy for table
-- asset_concept_tags" error.
--
-- Fix: mark the function SECURITY DEFINER so it runs with the function
-- owner's privileges, bypassing RLS for this one internal, fully
-- derived write — it never uses attacker/caller-supplied data beyond
-- what the outer, already-RLS-checked assets insert already committed.
-- ============================================================

CREATE OR REPLACE FUNCTION public.seed_asset_concept_tag()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO public.asset_concept_tags (asset_id, concept_id)
  VALUES (NEW.id, NEW.concept_id)
  ON CONFLICT DO NOTHING;
  RETURN NEW;
END;
$$;
