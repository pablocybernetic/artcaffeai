-- ============================================================
-- ARTCAFFE — Cross-worker lock for in-process schedulers
--
-- artcaffe-api.service runs uvicorn with multiple workers (API_WORKERS,
-- default 2). Each worker process is a full, independent copy of the
-- FastAPI app — including its lifespan — so master_scheduler,
-- reminder_scheduler, and meta_sync_scheduler all started TWICE, once
-- per worker, both firing on the same wall-clock schedule. Harmless
-- for master_scheduler (idempotent health snapshot) but a real
-- correctness bug for reminder_scheduler: two workers could both pass
-- the "not already reminded" check before either recorded it, sending
-- the same person the same reminder twice. meta_sync_scheduler would
-- likewise hit Meta's API twice as often as intended.
--
-- A tiny table + atomic UPDATE ... WHERE gives every worker a fair,
-- race-free way to ask "is it my turn?" without needing raw SQL/RPC —
-- Postgres serializes concurrent UPDATEs to the same row, so only one
-- of two simultaneous claim attempts can ever see locked_until in the
-- past and successfully bump it into the future.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.scheduler_locks (
  name text PRIMARY KEY,
  locked_until timestamptz NOT NULL DEFAULT '1970-01-01T00:00:00Z'
);

ALTER TABLE public.scheduler_locks ENABLE ROW LEVEL SECURITY;
-- Service-role only (bypasses RLS entirely) — no policies needed since
-- no authenticated/anon client should ever touch this table.
