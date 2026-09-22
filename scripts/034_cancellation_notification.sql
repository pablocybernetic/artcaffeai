-- 034_cancellation_notification.sql
-- Per-channel bookkeeping for the customer cancellation notification,
-- fired only when an admin opts in via the cancel confirmation dialog
-- (not automatic — matches the existing per-channel error tracking
-- pattern used for every other notification type on this table).

alter table public.table_bookings
  add column cancellation_email_sent boolean not null default false,
  add column cancellation_email_error text,
  add column cancellation_sms_sent boolean not null default false,
  add column cancellation_sms_error text;
