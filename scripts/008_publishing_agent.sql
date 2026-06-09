-- ============================================================
-- ARTCAFFE — Publishing Agent Schema Changes
-- Run in the Supabase SQL editor.
-- ============================================================

-- 1. Add scheduled_at to content_items
--    Stores the scheduled publish time set by the Publishing Agent.
ALTER TABLE public.content_items
  ADD COLUMN IF NOT EXISTS scheduled_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_content_items_scheduled_at
  ON public.content_items (scheduled_at)
  WHERE scheduled_at IS NOT NULL;

-- 2. Add scheduled_publish to the agent_type enum
--    ALTER TYPE ... ADD VALUE cannot run inside a transaction,
--    so it must be run as a standalone statement.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum
    WHERE enumlabel = 'scheduled_publish'
      AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'agent_type')
  ) THEN
    ALTER TYPE agent_type ADD VALUE 'scheduled_publish';
  END IF;
END;
$$;
