-- Per-location toggle for whether a branch appears as bookable on the
-- public dine-in form (frontend/src/routes/book-table.tsx). Defaults to
-- true so existing locations keep behaving as before until explicitly
-- turned off.
alter table public.locations
  add column accepts_table_booking boolean not null default true;

-- Seed: Restaurant + Gastro Bar branches take dine-in bookings; Market
-- branches (grocery/café takeaway concept) do not.
update public.locations
  set accepts_table_booking = true
  where brand_type in ('artcaffe_restaurant', 'artcaffe_gastro_bar');

update public.locations
  set accepts_table_booking = false
  where brand_type = 'artcaffe_market';
