-- ai_error_log: current AI/agent error state, keyed by source.
--
-- One row per source (e.g. "anthropic", "openai") holding its MOST RECENT
-- failure. The backend deletes a source's row the next time that same
-- provider call succeeds — so the frontend's announcement bar naturally
-- disappears once the underlying issue (e.g. depleted API credits) is
-- resolved, without any manual dismissal.

create table if not exists public.ai_error_log (
  source text primary key,
  message text not null,
  created_at timestamptz not null default now()
);

alter table public.ai_error_log enable row level security;

create policy "ai_error_log_select" on public.ai_error_log
  FOR SELECT TO authenticated USING (true);
