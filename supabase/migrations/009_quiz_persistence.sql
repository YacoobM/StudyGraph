begin;

create table public.quiz_sets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  graph_id uuid not null references public.graphs (id) on delete cascade,
  graph_version_id uuid not null references public.graph_versions (id) on delete cascade,
  status text not null default 'ready' check (status in ('generating', 'ready', 'empty')),
  created_at timestamptz not null default now(),
  unique (user_id, graph_version_id)
);

create table public.quiz_questions (
  id uuid primary key default gen_random_uuid(),
  quiz_set_id uuid not null references public.quiz_sets (id) on delete cascade,
  question_order integer not null,
  question text not null,
  options jsonb not null,
  correct_index integer not null,
  explanation text not null default '',
  difficulty text not null default 'medium' check (difficulty in ('easy', 'medium', 'hard')),
  unique (quiz_set_id, question_order)
);

create table public.quiz_attempts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  quiz_set_id uuid not null references public.quiz_sets (id) on delete cascade,
  correct integer not null check (correct >= 0),
  total integer not null check (total > 0),
  points integer not null default 0 check (points >= 0),
  passed boolean not null default false,
  created_at timestamptz not null default now()
);

create index quiz_sets_graph_version_idx on public.quiz_sets (graph_version_id);
create index quiz_questions_set_idx on public.quiz_questions (quiz_set_id, question_order);
create index quiz_attempts_user_set_idx on public.quiz_attempts (user_id, quiz_set_id, created_at desc);

alter table public.quiz_sets enable row level security;
alter table public.quiz_questions enable row level security;
alter table public.quiz_attempts enable row level security;

create policy quiz_sets_own on public.quiz_sets for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy quiz_questions_own on public.quiz_questions for all using (
  exists (select 1 from public.quiz_sets s where s.id = quiz_questions.quiz_set_id and s.user_id = auth.uid())
) with check (
  exists (select 1 from public.quiz_sets s where s.id = quiz_questions.quiz_set_id and s.user_id = auth.uid())
);
create policy quiz_attempts_own on public.quiz_attempts for all using (user_id = auth.uid()) with check (user_id = auth.uid());

commit;
