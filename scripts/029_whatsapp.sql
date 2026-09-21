-- 029_whatsapp.sql
-- WhatsApp Phase 1: template broadcasts, contacts/audience, campaign sends.
-- Inbound webhook (Phase 2) is out of scope here — whatsapp_messages exists
-- now so that later work has nowhere else new to touch besides the route.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'whatsapp_campaign_send' AND enumtypid = 'agent_type'::regtype) THEN
    ALTER TYPE agent_type ADD VALUE 'whatsapp_campaign_send';
  END IF;
END $$;

CREATE TABLE public.whatsapp_contacts (
  id uuid primary key default gen_random_uuid(),
  concept_id uuid references public.concepts(id),
  phone_number text not null,
  name text,
  opt_in boolean not null default false,
  opted_in_at timestamptz,
  tags text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (concept_id, phone_number)
);

CREATE TABLE public.whatsapp_campaign_sends (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.jobs(id),
  contact_id uuid not null references public.whatsapp_contacts(id),
  template_name text not null,
  status text not null default 'pending' check (status in ('pending','sent','delivered','read','failed')),
  wa_message_id text,
  error_message text,
  sent_at timestamptz,
  created_at timestamptz not null default now()
);
create index whatsapp_campaign_sends_job_id_idx on public.whatsapp_campaign_sends(job_id);

CREATE TABLE public.whatsapp_messages (
  id uuid primary key default gen_random_uuid(),
  direction text not null check (direction in ('inbound','outbound')),
  contact_id uuid references public.whatsapp_contacts(id),
  wa_message_id text,
  campaign_send_id uuid references public.whatsapp_campaign_sends(id),
  body text,
  status text,
  error_message text,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.whatsapp_contacts enable row level security;
alter table public.whatsapp_campaign_sends enable row level security;
alter table public.whatsapp_messages enable row level security;
-- Zero RLS policies — service-role only, matching every table in this codebase.
