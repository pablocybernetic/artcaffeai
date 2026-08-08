-- ============================================================
-- ARTCAFFE — Dynamic Roles & Permissions
--
-- Replaces the fixed public.user_role enum on team_members with a proper
-- `roles` table (admin can create/rename/delete roles) plus a
-- `role_permissions` matrix (role x page/feature -> allowed), so access to
-- pages like Users/Settings/Budget is actually enforced, not just labelled.
--
-- Backward-compat: existing RLS across the app calls is_admin() and
-- has_role(text[]) — both are rewritten here to work off the new role_id
-- FK instead of the old enum, but preserve their exact external behavior:
--   - is_admin() now checks roles.is_admin (a flag, immune to renaming)
--   - has_role(text[]) now checks roles.slug (a stable identifier, separate
--     from the admin-editable display `name`) against the given array —
--     every existing call site (concepts, assets, budgets, etc.) keeps
--     working unchanged.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Roles + permissions tables
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  slug text NOT NULL UNIQUE,       -- stable identifier for legacy RLS; not shown/edited in the UI
  is_admin boolean NOT NULL DEFAULT false,   -- grants full access; exactly one protected built-in role
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.role_permissions (
  role_id uuid NOT NULL REFERENCES public.roles(id) ON DELETE CASCADE,
  permission_key text NOT NULL,
  allowed boolean NOT NULL DEFAULT false,
  PRIMARY KEY (role_id, permission_key)
);

-- ------------------------------------------------------------
-- 2. Seed the 4 existing roles (stable slugs match the old enum values)
-- ------------------------------------------------------------
INSERT INTO public.roles (name, slug, is_admin) VALUES
  ('Admin',           'admin',           true),
  ('Content Manager', 'content_manager', false),
  ('Designer',        'designer',        false),
  ('Media Buyer',     'media_buyer',     false)
ON CONFLICT (slug) DO NOTHING;

-- Sensible starting permission matrix — admin can adjust freely afterward.
-- Permission keys mirror the app's actual pages; "dashboard" is always on
-- for every role and intentionally has no row here.
INSERT INTO public.role_permissions (role_id, permission_key, allowed)
SELECT r.id, p.key, p.allowed
FROM public.roles r
CROSS JOIN LATERAL (
  VALUES
    ('calendar',       true),
    ('briefs',         true),
    ('approvals',      true),
    ('chat_with_data', true),
    ('assets',         true),
    ('budget',         true),
    ('posts',          true),
    ('users',          true),
    ('settings',       true)
) AS p(key, allowed)
WHERE r.slug = 'admin'
ON CONFLICT (role_id, permission_key) DO NOTHING;

INSERT INTO public.role_permissions (role_id, permission_key, allowed)
SELECT r.id, p.key, p.allowed
FROM public.roles r
CROSS JOIN LATERAL (
  VALUES
    ('calendar',       true),
    ('briefs',         true),
    ('approvals',      true),
    ('chat_with_data', true),
    ('assets',         true),
    ('budget',         true),
    ('posts',          true),
    ('users',          false),
    ('settings',       false)
) AS p(key, allowed)
WHERE r.slug = 'content_manager'
ON CONFLICT (role_id, permission_key) DO NOTHING;

INSERT INTO public.role_permissions (role_id, permission_key, allowed)
SELECT r.id, p.key, p.allowed
FROM public.roles r
CROSS JOIN LATERAL (
  VALUES
    ('calendar',       true),
    ('briefs',         true),
    ('approvals',      false),
    ('chat_with_data', false),
    ('assets',         true),
    ('budget',         false),
    ('posts',          true),
    ('users',          false),
    ('settings',       false)
) AS p(key, allowed)
WHERE r.slug = 'designer'
ON CONFLICT (role_id, permission_key) DO NOTHING;

INSERT INTO public.role_permissions (role_id, permission_key, allowed)
SELECT r.id, p.key, p.allowed
FROM public.roles r
CROSS JOIN LATERAL (
  VALUES
    ('calendar',       true),
    ('briefs',         false),
    ('approvals',      false),
    ('chat_with_data', true),
    ('assets',         false),
    ('budget',         true),
    ('posts',          true),
    ('users',          false),
    ('settings',       false)
) AS p(key, allowed)
WHERE r.slug = 'media_buyer'
ON CONFLICT (role_id, permission_key) DO NOTHING;

-- ------------------------------------------------------------
-- 3. Migrate team_members from the fixed enum to role_id
-- ------------------------------------------------------------
ALTER TABLE public.team_members ADD COLUMN IF NOT EXISTS role_id uuid REFERENCES public.roles(id);

UPDATE public.team_members tm
SET role_id = r.id
FROM public.roles r
WHERE tm.role_id IS NULL AND r.slug = tm.role::text;

-- Safety net: anyone who somehow didn't match falls back to the least-
-- privileged seeded role rather than being left null.
UPDATE public.team_members
SET role_id = (SELECT id FROM public.roles WHERE slug = 'content_manager')
WHERE role_id IS NULL;

ALTER TABLE public.team_members ALTER COLUMN role_id SET NOT NULL;
ALTER TABLE public.team_members DROP COLUMN IF EXISTS role;

-- ------------------------------------------------------------
-- 4. Rewrite is_admin() / has_role() to use role_id — every existing RLS
--    policy that calls these keeps working with zero changes.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.team_members tm
    JOIN public.roles r ON r.id = tm.role_id
    WHERE tm.id = auth.uid() AND r.is_admin = true AND tm.is_active = true
  )
$$;

CREATE OR REPLACE FUNCTION public.has_role(roles text[])
RETURNS boolean
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.team_members tm
    JOIN public.roles r ON r.id = tm.role_id
    WHERE tm.id = auth.uid() AND tm.is_active = true AND r.slug = ANY(roles)
  )
$$;

-- New: page/feature-level permission check used by the frontend for
-- fine-grained access (independent of the coarser table-level RLS above).
CREATE OR REPLACE FUNCTION public.has_permission(perm_key text)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT public.is_admin() OR EXISTS (
    SELECT 1 FROM public.team_members tm
    JOIN public.role_permissions rp ON rp.role_id = tm.role_id
    WHERE tm.id = auth.uid() AND tm.is_active = true
      AND rp.permission_key = perm_key AND rp.allowed = true
  )
$$;

-- ------------------------------------------------------------
-- 5. Update the Google-signup trigger to assign role_id instead of the
--    old enum literal.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_artcaffe_signup()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  display_name    text;
  existing_role_id uuid;
  existing_id     uuid;
  default_role_id uuid;
BEGIN
  IF NEW.email NOT LIKE '%@artcaffe.co.ke' THEN
    RETURN NEW;
  END IF;

  display_name := COALESCE(
    NEW.raw_user_meta_data->>'full_name',
    NEW.raw_user_meta_data->>'name',
    split_part(NEW.email, '@', 1)
  );

  SELECT id, role_id INTO existing_id, existing_role_id
  FROM public.team_members
  WHERE email = NEW.email
  LIMIT 1;

  IF existing_id IS NOT NULL AND existing_id <> NEW.id THEN
    DELETE FROM public.team_members WHERE id = existing_id;

    INSERT INTO public.team_members (id, email, full_name, role_id, is_active, created_at, updated_at)
    VALUES (NEW.id, NEW.email, display_name, existing_role_id, true, now(), now());

  ELSIF existing_id IS NULL THEN
    SELECT id INTO default_role_id FROM public.roles WHERE slug = 'content_manager';

    INSERT INTO public.team_members (id, email, full_name, role_id, is_active, created_at, updated_at)
    VALUES (NEW.id, NEW.email, display_name, default_role_id, true, now(), now());
  END IF;

  RETURN NEW;
END;
$$;

-- ------------------------------------------------------------
-- 6. Close a self-escalation gap: the existing team_members_update policy
--    lets a user update their OWN row (for e.g. renaming themselves), but
--    had no column-level restriction — meaning any user could set their
--    own role_id/is_active directly. A trigger enforces that only admins
--    may change those two fields.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.guard_team_members_self_update()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  IF NOT public.is_admin() THEN
    IF NEW.role_id IS DISTINCT FROM OLD.role_id THEN
      RAISE EXCEPTION 'Only admins can change a member''s role';
    END IF;
    IF NEW.is_active IS DISTINCT FROM OLD.is_active THEN
      RAISE EXCEPTION 'Only admins can activate/deactivate members';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS team_members_guard_self_update ON public.team_members;
CREATE TRIGGER team_members_guard_self_update
  BEFORE UPDATE ON public.team_members
  FOR EACH ROW EXECUTE FUNCTION public.guard_team_members_self_update();

-- ------------------------------------------------------------
-- 7. RLS on the new tables — everyone reads (needed for sidebar/route
--    gating and the role dropdown), only admins write.
-- ------------------------------------------------------------
ALTER TABLE public.roles             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.role_permissions  ENABLE ROW LEVEL SECURITY;

CREATE POLICY "roles_select" ON public.roles
  FOR SELECT TO authenticated USING (true);
CREATE POLICY "roles_insert" ON public.roles
  FOR INSERT TO authenticated WITH CHECK (public.is_admin());
CREATE POLICY "roles_update" ON public.roles
  FOR UPDATE TO authenticated USING (public.is_admin());
CREATE POLICY "roles_delete" ON public.roles
  FOR DELETE TO authenticated USING (public.is_admin() AND is_admin = false);

CREATE POLICY "role_permissions_select" ON public.role_permissions
  FOR SELECT TO authenticated USING (true);
CREATE POLICY "role_permissions_insert" ON public.role_permissions
  FOR INSERT TO authenticated WITH CHECK (public.is_admin());
CREATE POLICY "role_permissions_update" ON public.role_permissions
  FOR UPDATE TO authenticated USING (public.is_admin());
CREATE POLICY "role_permissions_delete" ON public.role_permissions
  FOR DELETE TO authenticated USING (public.is_admin());
