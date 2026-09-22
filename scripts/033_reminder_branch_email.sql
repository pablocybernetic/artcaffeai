-- 033_reminder_branch_email.sql
-- The branch also gets a reminder email, same as the customer does,
-- ahead of the reservation.

alter table public.table_bookings
  add column reminder_branch_email_sent boolean not null default false,
  add column reminder_branch_email_error text;
