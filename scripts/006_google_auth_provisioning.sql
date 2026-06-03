-- ============================================================
-- ARTCAFFE — Google OAuth Auto-Provisioning
-- Run in Supabase SQL Editor after enabling Google provider.
--
-- Handles two cases:
--   A) User already has a placeholder row in team_members
--      (fake UUID like 11111111-...) → replaces the old row
--      with the real auth UUID, preserving their role.
--   B) Brand new @artcaffe.co.ke user → inserts with
--      role = content_manager.
--
-- Non-artcaffe.co.ke emails are ignored entirely.
-- ============================================================

CREATE OR REPLACE FUNCTION public.handle_artcaffe_signup()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  display_name  text;
  existing_role text;
  existing_id   uuid;
BEGIN
  -- Only process @artcaffe.co.ke addresses
  IF NEW.email NOT LIKE '%@artcaffe.co.ke' THEN
    RETURN NEW;
  END IF;

  -- Prefer Google display name, fall back to email prefix
  display_name := COALESCE(
    NEW.raw_user_meta_data->>'full_name',
    NEW.raw_user_meta_data->>'name',
    split_part(NEW.email, '@', 1)
  );

  -- Check for existing row by email (may have a placeholder UUID)
  SELECT id, role INTO existing_id, existing_role
  FROM public.team_members
  WHERE email = NEW.email
  LIMIT 1;

  IF existing_id IS NOT NULL AND existing_id <> NEW.id THEN
    -- Case A: placeholder row exists with a different UUID.
    -- Delete old row and re-insert with the real auth UUID,
    -- keeping the existing role so admins stay admin.
    DELETE FROM public.team_members WHERE id = existing_id;

    INSERT INTO public.team_members (id, email, full_name, role, is_active, created_at, updated_at)
    VALUES (
      NEW.id,
      NEW.email,
      display_name,
      existing_role,   -- preserve existing role (admin/content_manager/etc.)
      true,
      now(),
      now()
    );

  ELSIF existing_id IS NULL THEN
    -- Case B: no row at all — new team member, default to content_manager
    INSERT INTO public.team_members (id, email, full_name, role, is_active, created_at, updated_at)
    VALUES (
      NEW.id,
      NEW.email,
      display_name,
      'content_manager',
      true,
      now(),
      now()
    );

  END IF;
  -- Case C: existing_id = NEW.id → row already correct, do nothing

  RETURN NEW;
END;
$$;

-- Idempotent: drop and recreate trigger
DROP TRIGGER IF EXISTS on_artcaffe_user_created ON auth.users;

CREATE TRIGGER on_artcaffe_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_artcaffe_signup();
