begin;

create table public.notes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  graph_id uuid not null references public.graphs (id) on delete cascade,
  graph_version_id uuid not null references public.graph_versions (id) on delete cascade,
  title text not null,
  kind text not null default 'full' check (kind in ('full', 'plan')),
  days integer,
  status text not null default 'current' check (status in ('current', 'stale')),
  document_ids jsonb not null default '[]'::jsonb,
  documents jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, graph_id)
);

create table public.note_sections (
  id uuid primary key default gen_random_uuid(),
  note_id uuid not null references public.notes (id) on delete cascade,
  section_order integer not null,
  day integer,
  title text not null,
  concepts jsonb not null default '[]'::jsonb,
  relations jsonb not null default '[]'::jsonb,
  sources jsonb not null default '[]'::jsonb,
  must_cover jsonb not null default '[]'::jsonb,
  body jsonb,
  done boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (note_id, section_order)
);

create index notes_graph_version_idx on public.notes (graph_id, graph_version_id);
create index notes_user_updated_idx on public.notes (user_id, updated_at desc);
create index note_sections_note_order_idx on public.note_sections (note_id, section_order);

alter table public.notes enable row level security;
alter table public.note_sections enable row level security;

create policy notes_own on public.notes
for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy note_sections_own on public.note_sections
for all using (
  exists (select 1 from public.notes n where n.id = note_sections.note_id and n.user_id = auth.uid())
) with check (
  exists (select 1 from public.notes n where n.id = note_sections.note_id and n.user_id = auth.uid())
);

commit;
