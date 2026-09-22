-- 031_table_booking_reminders.sql
-- Per-channel bookkeeping for the pre-reservation reminder scheduler,
-- matching the exact same pattern as the confirmation-notification
-- columns added in 030_table_bookings.sql (sms_sent/sms_error, etc.).

alter table public.table_bookings
  add column reminder_email_sent boolean not null default false,
  add column reminder_email_error text,
  add column reminder_sms_sent boolean not null default false,
  add column reminder_sms_error text,
  add column reminder_sent_at timestamptz;
