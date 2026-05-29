-- ============================================================
-- ARTCAFFE — Row Level Security Policies
-- Run in the Supabase SQL editor AFTER the main schema.
--
-- Strategy:
--   • Service role (FastAPI backend) bypasses RLS automatically.
--   • Authenticated frontend users are filtered by:
--       - is_admin()        → admin role sees/writes everything
--       - my_concept_ids()  → non-admins filtered to their assigned concepts
--   • Role hierarchy: admin > content_manager > designer > media_buyer
-- ============================================================

-- ============================================================
-- Helper functions (SECURITY DEFINER so they run as owner,
-- not as the calling user — avoids RLS recursion)
-- ============================================================

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.team_members
    WHERE id = auth.uid() AND role = 'admin' AND is_active = true
  )
$$;

-- Returns the concept UUIDs the current user is assigned to.
-- Falls back to empty array if not in team_members.
CREATE OR REPLACE FUNCTION public.my_concept_ids()
RETURNS uuid[]
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT COALESCE(
    ARRAY(
      SELECT concept_id FROM public.member_concept_access
      WHERE member_id = auth.uid()
    ),
    '{}'::uuid[]
  )
$$;

-- Returns true if the current user has one of the given roles.
CREATE OR REPLACE FUNCTION public.has_role(roles text[])
RETURNS boolean
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.team_members
    WHERE id = auth.uid() AND is_active = true AND role::text = ANY(roles)
  )
$$;


-- ============================================================
-- Enable RLS on every table
-- ============================================================
ALTER TABLE public.concepts                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.team_members            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.member_concept_access   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_briefs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_items           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.assets                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.calendar_entries        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.budget_allocations      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.budget_alerts           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.platform_data_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.brand_contexts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.research_briefs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_notifications     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.feedback_comments       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.approval_events         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.published_posts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.platform_credentials    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profiles                ENABLE ROW LEVEL SECURITY;


-- ============================================================
-- concepts — all authenticated users can read; admins write
-- ============================================================
CREATE POLICY "concepts_select" ON public.concepts
  FOR SELECT TO authenticated USING (true);

CREATE POLICY "concepts_insert" ON public.concepts
  FOR INSERT TO authenticated WITH CHECK (public.is_admin());

CREATE POLICY "concepts_update" ON public.concepts
  FOR UPDATE TO authenticated USING (public.is_admin());

CREATE POLICY "concepts_delete" ON public.concepts
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- team_members — everyone reads roster; admins write; users update self
-- ============================================================
CREATE POLICY "team_members_select" ON public.team_members
  FOR SELECT TO authenticated USING (true);

CREATE POLICY "team_members_insert" ON public.team_members
  FOR INSERT TO authenticated WITH CHECK (public.is_admin());

CREATE POLICY "team_members_update" ON public.team_members
  FOR UPDATE TO authenticated
  USING (public.is_admin() OR id = auth.uid());

CREATE POLICY "team_members_delete" ON public.team_members
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- member_concept_access — admins manage; users read own assignments
-- ============================================================
CREATE POLICY "mca_select" ON public.member_concept_access
  FOR SELECT TO authenticated
  USING (public.is_admin() OR member_id = auth.uid());

CREATE POLICY "mca_insert" ON public.member_concept_access
  FOR INSERT TO authenticated WITH CHECK (public.is_admin());

CREATE POLICY "mca_update" ON public.member_concept_access
  FOR UPDATE TO authenticated USING (public.is_admin());

CREATE POLICY "mca_delete" ON public.member_concept_access
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- content_briefs
-- SELECT  → all authenticated in their concepts
-- INSERT/UPDATE → admin + content_manager only
-- DELETE  → admin only
-- ============================================================
CREATE POLICY "briefs_select" ON public.content_briefs
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "briefs_insert" ON public.content_briefs
  FOR INSERT TO authenticated
  WITH CHECK (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager'])
  );

CREATE POLICY "briefs_update" ON public.content_briefs
  FOR UPDATE TO authenticated
  USING (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager'])
  );

CREATE POLICY "briefs_delete" ON public.content_briefs
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- content_items — scoped via parent brief's concept
-- ============================================================
CREATE POLICY "items_select" ON public.content_items
  FOR SELECT TO authenticated
  USING (
    public.is_admin() OR
    brief_id IN (
      SELECT id FROM public.content_briefs
      WHERE concept_id = ANY(public.my_concept_ids())
    )
  );

CREATE POLICY "items_insert" ON public.content_items
  FOR INSERT TO authenticated
  WITH CHECK (
    brief_id IN (
      SELECT id FROM public.content_briefs
      WHERE concept_id = ANY(public.my_concept_ids())
    )
    AND public.has_role(ARRAY['admin', 'content_manager'])
  );

CREATE POLICY "items_update" ON public.content_items
  FOR UPDATE TO authenticated
  USING (
    brief_id IN (
      SELECT id FROM public.content_briefs
      WHERE concept_id = ANY(public.my_concept_ids())
    )
    AND public.has_role(ARRAY['admin', 'content_manager'])
  );

CREATE POLICY "items_delete" ON public.content_items
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- assets — admins + content_managers + designers can write
-- ============================================================
CREATE POLICY "assets_select" ON public.assets
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "assets_insert" ON public.assets
  FOR INSERT TO authenticated
  WITH CHECK (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
  );

CREATE POLICY "assets_update" ON public.assets
  FOR UPDATE TO authenticated
  USING (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager', 'designer'])
  );

CREATE POLICY "assets_delete" ON public.assets
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- calendar_entries — content_managers write, all roles read
-- ============================================================
CREATE POLICY "calendar_select" ON public.calendar_entries
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "calendar_insert" ON public.calendar_entries
  FOR INSERT TO authenticated
  WITH CHECK (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager'])
  );

CREATE POLICY "calendar_update" ON public.calendar_entries
  FOR UPDATE TO authenticated
  USING (
    concept_id = ANY(public.my_concept_ids())
    AND public.has_role(ARRAY['admin', 'content_manager'])
  );

CREATE POLICY "calendar_delete" ON public.calendar_entries
  FOR DELETE TO authenticated USING (public.is_admin());


-- ============================================================
-- budget_allocations — media_buyers read; admins write
-- ============================================================
CREATE POLICY "budget_alloc_select" ON public.budget_allocations
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "budget_alloc_write" ON public.budget_allocations
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- budget_alerts — scoped via parent allocation's concept
-- ============================================================
CREATE POLICY "budget_alerts_select" ON public.budget_alerts
  FOR SELECT TO authenticated
  USING (
    public.is_admin() OR
    allocation_id IN (
      SELECT id FROM public.budget_allocations
      WHERE concept_id = ANY(public.my_concept_ids())
    )
  );

CREATE POLICY "budget_alerts_write" ON public.budget_alerts
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- platform_data_snapshots — all roles read own concepts;
-- FastAPI (service role) writes via bypass
-- ============================================================
CREATE POLICY "snapshots_select" ON public.platform_data_snapshots
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

-- Writes come from FastAPI (service role, bypasses RLS).
-- Allow admins to insert/update from the UI as well.
CREATE POLICY "snapshots_write" ON public.platform_data_snapshots
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- brand_contexts — all roles read own concepts; admins write
-- ============================================================
CREATE POLICY "brand_ctx_select" ON public.brand_contexts
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "brand_ctx_write" ON public.brand_contexts
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- jobs — read own concepts; writes from FastAPI (service role)
-- ============================================================
CREATE POLICY "jobs_select" ON public.jobs
  FOR SELECT TO authenticated
  USING (
    public.is_admin() OR concept_id = ANY(public.my_concept_ids())
  );

CREATE POLICY "jobs_write" ON public.jobs
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- research_briefs — read own concepts; admins write
-- ============================================================
CREATE POLICY "research_select" ON public.research_briefs
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "research_write" ON public.research_briefs
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- notifications — each recipient sees only their own rows
-- ============================================================
CREATE POLICY "notifications_select" ON public.notifications
  FOR SELECT TO authenticated
  USING (public.is_admin() OR recipient_id = auth.uid());

-- Writes come from FastAPI (service role, bypasses RLS).
CREATE POLICY "notifications_write" ON public.notifications
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- agent_notifications — system audit log; admins only
-- ============================================================
CREATE POLICY "agent_notif_select" ON public.agent_notifications
  FOR SELECT TO authenticated USING (public.is_admin());

CREATE POLICY "agent_notif_write" ON public.agent_notifications
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- feedback_comments — scoped via brief's concept; users edit own
-- ============================================================
CREATE POLICY "comments_select" ON public.feedback_comments
  FOR SELECT TO authenticated
  USING (
    public.is_admin() OR
    brief_id IN (
      SELECT id FROM public.content_briefs
      WHERE concept_id = ANY(public.my_concept_ids())
    )
  );

CREATE POLICY "comments_insert" ON public.feedback_comments
  FOR INSERT TO authenticated
  WITH CHECK (
    author_id = auth.uid() AND (
      public.is_admin() OR
      brief_id IN (
        SELECT id FROM public.content_briefs
        WHERE concept_id = ANY(public.my_concept_ids())
      )
    )
  );

CREATE POLICY "comments_update" ON public.feedback_comments
  FOR UPDATE TO authenticated
  USING (public.is_admin() OR author_id = auth.uid());

CREATE POLICY "comments_delete" ON public.feedback_comments
  FOR DELETE TO authenticated
  USING (public.is_admin() OR author_id = auth.uid());


-- ============================================================
-- approval_events — anyone on the concept can read; only reviewers insert
-- ============================================================
CREATE POLICY "approval_select" ON public.approval_events
  FOR SELECT TO authenticated
  USING (
    public.is_admin() OR
    brief_id IN (
      SELECT id FROM public.content_briefs
      WHERE concept_id = ANY(public.my_concept_ids())
    )
  );

CREATE POLICY "approval_insert" ON public.approval_events
  FOR INSERT TO authenticated
  WITH CHECK (
    reviewer_id = auth.uid() AND
    public.has_role(ARRAY['admin', 'content_manager'])
  );


-- ============================================================
-- published_posts — read own concepts; admins write
-- ============================================================
CREATE POLICY "published_select" ON public.published_posts
  FOR SELECT TO authenticated
  USING (public.is_admin() OR concept_id = ANY(public.my_concept_ids()));

CREATE POLICY "published_write" ON public.published_posts
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- platform_credentials — admins only (contains tokens)
-- ============================================================
CREATE POLICY "platform_creds_select" ON public.platform_credentials
  FOR SELECT TO authenticated USING (public.is_admin());

CREATE POLICY "platform_creds_write" ON public.platform_credentials
  FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- ============================================================
-- profiles — users see own; admins see all
-- ============================================================
CREATE POLICY "profiles_select" ON public.profiles
  FOR SELECT TO authenticated
  USING (public.is_admin() OR id = auth.uid());

CREATE POLICY "profiles_write" ON public.profiles
  FOR ALL TO authenticated
  USING (public.is_admin() OR id = auth.uid())
  WITH CHECK (public.is_admin() OR id = auth.uid());
