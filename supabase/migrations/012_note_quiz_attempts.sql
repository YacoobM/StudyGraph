begin;

create table public.note_quiz_attempts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  graph_id uuid not null references public.graphs (id) on delete cascade,
  note_id uuid not null references public.notes (id) on delete cascade,
  section_id uuid not null references public.note_sections (id) on delete cascade,
  status text not null default 'completed'
    check (status in ('in_progress', 'completed')),
  current_index integer not null default 0 check (current_index >= 0),
  answers jsonb not null default '{}'::jsonb,
  correct integer not null default 0 check (correct >= 0),
  total integer not null check (total > 0),
  points integer not null default 0 check (points >= 0),
  passed boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index note_quiz_attempts_active_idx
  on public.note_quiz_attempts (user_id, section_id)
  where status = 'in_progress';
create index note_quiz_attempts_section_idx
  on public.note_quiz_attempts (user_id, section_id, updated_at desc);

alter table public.note_quiz_attempts enable row level security;
create policy note_quiz_attempts_own on public.note_quiz_attempts
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

commit;
