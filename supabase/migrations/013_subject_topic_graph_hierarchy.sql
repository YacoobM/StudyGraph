-- Persist the sidebar hierarchy as Subject -> Topic -> Graph.
--
-- topic_id is intentionally nullable during the transition. Existing clients
-- still create graphs before the Supabase-backed sidebar is installed. The
-- follow-up migration can make it NOT NULL once graph creation always supplies
-- a topic.

create table if not exists public.topics (
  id uuid primary key default gen_random_uuid(),
  subject_id uuid not null references public.subjects (id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (subject_id, name)
);

create index if not exists topics_subject_id_idx on public.topics (subject_id);

alter table public.topics enable row level security;

create policy topics_own on public.topics
for all to authenticated
using (
  exists (
    select 1
    from public.subjects s
    where s.id = topics.subject_id
      and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.subjects s
    where s.id = topics.subject_id
      and s.user_id = auth.uid()
  )
);

alter table public.graphs
  add column if not exists topic_id uuid references public.topics (id) on delete cascade;

create index if not exists graphs_topic_id_idx on public.graphs (topic_id);

create unique index if not exists graphs_topic_name_key
  on public.graphs (topic_id, name)
  where topic_id is not null;
