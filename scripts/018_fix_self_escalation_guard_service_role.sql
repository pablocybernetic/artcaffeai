-- ============================================================
-- ARTCAFFE — Fix guard_team_members_self_update() blocking legit admin writes
--
-- 015_dynamic_roles.sql's guard_team_members_self_update() (a trigger on
-- team_members) exists to stop a NON-admin from sneaking a role_id/
-- is_active change into a direct, browser-authenticated update of their
-- OWN row. It checks public.is_admin(), which resolves via auth.uid().
--
-- The app's actual role-change flow (updateMemberRole /
-- setMemberActive in team-members.functions.ts) already verifies the
-- caller is a real admin at the application layer (verifyAdmin(), reading
-- roles.is_admin off their own team_members row) BEFORE writing — but it
-- writes through the service-role Supabase client, which has no user JWT
-- and thus no auth.uid(). The trigger's is_admin() check evaluates to
-- false for every one of these already-authorized writes, so it always
-- raised "Only admins can change a member's role" — meaning NO admin,
-- through the app, could ever actually change anyone's role or
-- active status.
--
-- Fix: let the trigger trust service-role writes (only reachable through
-- our own server functions, which already gate on verifyAdmin()) and
-- keep enforcing the original check for anything else — i.e. a
-- non-admin end user attempting to write role_id/is_active directly via
-- the browser's authenticated client.
-- ============================================================

CREATE OR REPLACE FUNCTION public.guard_team_members_self_update()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  IF auth.role() = 'service_role' THEN
    RETURN NEW;
  END IF;

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
