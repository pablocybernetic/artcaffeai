-- ============================================================
-- ARTCAFFE — Google OAuth Auto-Provisioning
-- Run in Supabase SQL Editor after enabling Google provider.
--
-- Behaviour:
--   Any @artcaffe.co.ke email that signs in via Google is
--   automatically inserted into team_members with role
--   'content_manager' if they don't already have a row.
--   Existing rows (same id) are left untouched.
-- ============================================================

CREATE OR REPLACE FUNCTION public.handle_artcaffe_signup()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  display_name text;
BEGIN
  -- Only auto-provision @artcaffe.co.ke addresses
  IF NEW.email NOT LIKE '%@artcaffe.co.ke' THEN
    RETURN NEW;
  END IF;

  -- Prefer Google full_name, fall back to local part of email
  display_name := COALESCE(
    NEW.raw_user_meta_data->>'full_name',
    NEW.raw_user_meta_data->>'name',
    split_part(NEW.email, '@', 1)
  );

  INSERT INTO public.team_members (id, email, full_name, role, is_active, created_at, updated_at)
  VALUES (
    NEW.id,
    NEW.email,
    display_name,
    'content_manager',
    true,
    now(),
    now()
  )
  ON CONFLICT (id) DO NOTHING;  -- existing members keep their current role

  RETURN NEW;
END;
$$;

-- Drop if exists, then recreate (idempotent)
DROP TRIGGER IF EXISTS on_artcaffe_user_created ON auth.users;

CREATE TRIGGER on_artcaffe_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_artcaffe_signup();
