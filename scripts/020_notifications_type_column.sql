-- ============================================================
-- ARTCAFFE — Add a `type` column to notifications
--
-- notifications (the per-recipient inbox table) never recorded which
-- kind of event it was for — only agent_notifications (the separate
-- audit log) had that. The new reminder scheduler needs to tell "have
-- we already reminded this person about this post" apart from every
-- other notification a content_item might have, which requires
-- filtering by type.
-- ============================================================

ALTER TABLE public.notifications
  ADD COLUMN IF NOT EXISTS type text;
