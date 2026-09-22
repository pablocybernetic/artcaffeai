-- 030_table_bookings.sql
-- Table booking submissions (dine-in reservation form, embedded via iframe
-- into the Shopify storefront by the user — this backend never touches the
-- Shopify theme itself). Service-role only, matching every table in this
-- codebase: RLS enabled, zero policies.

CREATE TABLE public.table_bookings (
  id uuid primary key default gen_random_uuid(),
  location_id uuid not null references public.locations(id),
  customer_name text not null,
  party_size int not null check (party_size > 0),
  booking_date date not null,
  booking_time text not null,
  phone text not null,
  email text not null,
  special_occasion text,
  seating_preference text not null check (seating_preference in ('flexible','inside','outside')),
  status text not null default 'pending' check (status in ('pending','confirmed','declined','cancelled')),
  sms_sent boolean not null default false,
  sms_error text,
  email_sent boolean not null default false,
  email_error text,
  staff_notified boolean not null default false,
  staff_notify_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index table_bookings_location_id_idx on public.table_bookings(location_id);
create index table_bookings_status_idx on public.table_bookings(status);
create index table_bookings_booking_date_idx on public.table_bookings(booking_date);

alter table public.table_bookings enable row level security;
