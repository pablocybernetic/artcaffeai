-- ============================================================
-- ARTCAFFE — Add missing admin bypass to write (INSERT/UPDATE) policies
--
-- 003_rls_policies.sql's own header states the intended design:
--   "is_admin() → admin role sees/writes everything"
-- Every SELECT and DELETE policy on these tables already implements
-- that with `is_admin() OR ...`. The INSERT/UPDATE policies below were
-- written WITHOUT that bypass, requiring even an admin to have a row
-- in member_concept_access for the target concept. In practice no real
-- team member (only a seed/demo id) has ever had rows there, so every
-- direct-from-browser write to these tables — as any real user,
-- including admins — silently failed RLS. Postgrest/Postgres reports
-- "no error, 0 rows affected" for a failed UPDATE and a RLS violation
-- for a failed INSERT; both looked like unrelated bugs (an image
-- upload rejected, a drag-to-reschedule reverting after refresh)
-- before tracing back to this shared root cause.
--
-- Fix: add the same `is_admin() OR (...)` bypass already used on
-- every read/delete policy to the write policies that were missing it.
-- Non-admin access is completely unchanged — this only widens what
-- admins can do, matching the design the header already describes.
-- ============================================================

DROP POLICY IF EXISTS "briefs_insert" ON public.content_briefs;
CREATE POLICY "briefs_insert" ON public.content_briefs
  FOR INSERT TO authenticated
  WITH CHECK (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager'])
    )
  );

DROP POLICY IF EXISTS "briefs_update" ON public.content_briefs;
CREATE POLICY "briefs_update" ON public.content_briefs
  FOR UPDATE TO authenticated
  USING (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager'])
    )
  );

DROP POLICY IF EXISTS "items_insert" ON public.content_items;
CREATE POLICY "items_insert" ON public.content_items
  FOR INSERT TO authenticated
  WITH CHECK (
    public.is_admin() OR (
      brief_id IN (
        SELECT id FROM public.content_briefs
        WHERE concept_id = ANY(public.my_concept_ids())
      )
      AND public.has_role(ARRAY['admin', 'content_manager'])
    )
  );

DROP POLICY IF EXISTS "items_update" ON public.content_items;
CREATE POLICY "items_update" ON public.content_items
  FOR UPDATE TO authenticated
  USING (
    public.is_admin() OR (
      brief_id IN (
        SELECT id FROM public.content_briefs
        WHERE concept_id = ANY(public.my_concept_ids())
      )
      AND public.has_role(ARRAY['admin', 'content_manager'])
    )
  );

DROP POLICY IF EXISTS "assets_insert" ON public.assets;
CREATE POLICY "assets_insert" ON public.assets
  FOR INSERT TO authenticated
  WITH CHECK (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
    )
  );

DROP POLICY IF EXISTS "assets_update" ON public.assets;
CREATE POLICY "assets_update" ON public.assets
  FOR UPDATE TO authenticated
  USING (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
    )
  );

DROP POLICY IF EXISTS "calendar_insert" ON public.calendar_entries;
CREATE POLICY "calendar_insert" ON public.calendar_entries
  FOR INSERT TO authenticated
  WITH CHECK (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager'])
    )
  );

DROP POLICY IF EXISTS "calendar_update" ON public.calendar_entries;
CREATE POLICY "calendar_update" ON public.calendar_entries
  FOR UPDATE TO authenticated
  USING (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager'])
    )
  );

DROP POLICY IF EXISTS "asset_concept_tags_insert" ON public.asset_concept_tags;
CREATE POLICY "asset_concept_tags_insert" ON public.asset_concept_tags
  FOR INSERT TO authenticated
  WITH CHECK (
    public.is_admin() OR (
      concept_id = ANY(public.my_concept_ids())
      AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
    )
  );
