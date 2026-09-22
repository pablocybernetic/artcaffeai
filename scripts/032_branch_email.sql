-- 032_branch_email.sql
-- Per-branch contact email (e.g. westview@artcaffe.co.ke) so the
-- specific branch/manager gets notified of bookings at their own
-- location, independent of the admin team-wide notification and the
-- customer's own confirmation email.

alter table public.locations add column branch_email text;

alter table public.table_bookings
  add column branch_email_sent boolean not null default false,
  add column branch_email_error text;
