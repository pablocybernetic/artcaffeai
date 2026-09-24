-- Per-channel timestamps for the booking detail page's notification
-- timeline (frontend/src/routes/table-bookings_.$bookingId.tsx). Each is
-- set at attempt time regardless of success/failure, so a failed send
-- still has a place in the timeline. reminder_sent_at (031) already
-- covers the whole reminder batch (email+sms+branch email fire together
-- in one _send_reminder call) so no separate reminder timestamps needed.
alter table public.table_bookings
  add column email_sent_at timestamptz,
  add column sms_sent_at timestamptz,
  add column staff_notified_at timestamptz,
  add column branch_email_sent_at timestamptz,
  add column cancellation_email_sent_at timestamptz,
  add column cancellation_sms_sent_at timestamptz;
