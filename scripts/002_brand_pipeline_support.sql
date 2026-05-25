-- ============================================================
-- ARTCAFFE — Brand Pipeline Supporting SQL
-- Run these in the Supabase SQL editor after the main schema migration.
-- ============================================================


-- ============================================================
-- 1. claim_next_job function
-- Called by the job runner to atomically claim one pending job.
-- FOR UPDATE SKIP LOCKED prevents race conditions when multiple
-- worker instances are running.
-- ============================================================

create or replace function claim_next_job()
returns setof jobs
language sql
as $$
  update jobs
  set status = 'running',
      started_at = now()
  where id = (
    select id from jobs
    where status = 'pending'
      and retry_count < max_retries
    order by queued_at asc
    limit 1
    for update skip locked
  )
  returning *;
$$;


-- ============================================================
-- 2. Brand context cache invalidation trigger
-- When a new brand_context row is inserted with is_active = true,
-- this notifies the application layer via Supabase Realtime.
-- The BrandContextLoader in the agent process listens for this
-- and clears its in-memory cache.
-- ============================================================

-- Enable Realtime on brand_contexts
alter publication supabase_realtime add table brand_contexts;

-- The Next.js frontend and FastAPI workers subscribe to this channel:
-- supabase.channel('brand_contexts')
--   .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'brand_contexts' },
--     (payload) => { if (payload.new.is_active) invalidateBrandCache(payload.new.concept_id) })
--   .subscribe()


-- ============================================================
-- 3. Useful queries for the Settings module
-- ============================================================

-- Get brand context status for all three concepts at once
-- (used by the Settings page to show which concepts have guidelines uploaded)
select
  c.key,
  c.name,
  bc.version,
  bc.token_count,
  bc.source_file_path,
  bc.processed_by,
  bc.created_at as last_processed_at,
  case when bc.id is null then 'missing' else 'active' end as guidelines_status
from concepts c
left join brand_contexts bc
  on bc.concept_id = c.id
  and bc.is_active = true
order by c.key;


-- ============================================================
-- 4. Supabase Storage setup (run in Dashboard, not SQL editor)
-- ============================================================

-- Create the storage bucket via Supabase Dashboard or CLI:
--
-- supabase storage create brand-guidelines --public false
--
-- Bucket settings:
--   Name:          brand-guidelines
--   Public:        false  (never expose brand PDFs publicly)
--   File size limit: 20971520  (20 MB in bytes)
--   Allowed MIME types: application/pdf
--
-- Storage RLS policy — authenticated users with admin role can upload,
-- all authenticated users can read (agents need read access):
--
-- CREATE POLICY "Admins can upload brand guidelines"
-- ON storage.objects FOR INSERT TO authenticated
-- WITH CHECK (
--   bucket_id = 'brand-guidelines'
--   AND (
--     SELECT role FROM team_members WHERE id = auth.uid()
--   ) = 'admin'
-- );
--
-- CREATE POLICY "Authenticated users can read brand guidelines"
-- ON storage.objects FOR SELECT TO authenticated
-- USING (bucket_id = 'brand-guidelines');
