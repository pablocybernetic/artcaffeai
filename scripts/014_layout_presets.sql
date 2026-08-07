-- layout_presets: reusable headline/body/scrim STYLE presets for the Assets
-- page's layout editor. Global (not brand-scoped) since positioning/font
-- choices are visual style, not brand-specific content — a "Product Launch"
-- preset should apply the same way across Market/Restaurant/Gastro Bar.
--
-- Only style parameters are stored (position, size, color, align, font,
-- scrim) — never the literal headline/body TEXT, since that's specific to
-- whatever asset the preset is being applied to.

create table if not exists public.layout_presets (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  headline_x_pct numeric not null default 0.07,
  headline_y_pct numeric not null default 0.70,
  headline_size_pct numeric not null default 0.072,
  headline_color text not null default '#FFFFFF',
  headline_align text not null default 'left',
  headline_font text not null default '',
  body_x_pct numeric not null default 0.07,
  body_y_pct numeric not null default 0.85,
  body_size_pct numeric not null default 0.028,
  body_color text not null default '#FFFFFF',
  body_align text not null default 'left',
  body_font text not null default '',
  scrim_position text not null default 'none',
  scrim_height_pct numeric not null default 0.35,
  scrim_opacity numeric not null default 0.65,
  created_at timestamptz not null default now()
);

alter table public.layout_presets enable row level security;

create policy "layout_presets_select" on public.layout_presets
  FOR SELECT TO authenticated USING (true);

create policy "layout_presets_insert" on public.layout_presets
  FOR INSERT TO authenticated WITH CHECK (true);

create policy "layout_presets_delete" on public.layout_presets
  FOR DELETE TO authenticated USING (true);
