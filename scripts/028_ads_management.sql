ALTER TABLE public.platform_credentials ADD COLUMN IF NOT EXISTS ad_account_id text;

CREATE TABLE public.ad_campaigns (
  id uuid primary key default gen_random_uuid(),
  concept_id uuid references public.concepts(id),
  platform text not null check (platform in ('meta')),
  platform_campaign_id text,
  name text not null,
  objective text not null,
  status text not null default 'paused' check (status in ('paused','active','archived')),
  special_ad_categories jsonb not null default '[]'::jsonb,
  buying_type text not null default 'AUCTION',
  budget_amount numeric,
  budget_type text check (budget_type in ('daily','lifetime')),
  is_campaign_budget_optimization boolean not null default true,
  start_time timestamptz,
  end_time timestamptz,
  created_by text,
  spend numeric,
  last_synced_at timestamptz,
  last_sync_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index ad_campaigns_concept_id_idx on public.ad_campaigns(concept_id);

CREATE TABLE public.ad_sets (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.ad_campaigns(id) on delete cascade,
  platform_adset_id text,
  name text not null,
  status text not null default 'paused' check (status in ('paused','active','archived')),
  budget_amount numeric,
  budget_type text check (budget_type in ('daily','lifetime')),
  billing_event text not null default 'IMPRESSIONS',
  optimization_goal text,
  targeting jsonb not null default '{}'::jsonb,
  start_time timestamptz,
  end_time timestamptz,
  last_synced_at timestamptz,
  last_sync_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index ad_sets_campaign_id_idx on public.ad_sets(campaign_id);

CREATE TABLE public.ads (
  id uuid primary key default gen_random_uuid(),
  ad_set_id uuid not null references public.ad_sets(id) on delete cascade,
  platform_ad_id text,
  platform_creative_id text,
  name text not null,
  status text not null default 'paused' check (status in ('paused','active','archived')),
  asset_id uuid references public.assets(id),
  primary_text text,
  headline text,
  description text,
  call_to_action text,
  destination_url text,
  last_synced_at timestamptz,
  last_sync_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index ads_ad_set_id_idx on public.ads(ad_set_id);

alter table public.ad_campaigns enable row level security;
alter table public.ad_sets enable row level security;
alter table public.ads enable row level security;
