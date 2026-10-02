begin;

alter table public.quiz_attempts
  add column status text not null default 'completed'
    check (status in ('in_progress', 'completed')),
  add column current_index integer not null default 0 check (current_index >= 0),
  add column answers jsonb not null default '{}'::jsonb,
  add column updated_at timestamptz not null default now();

create index quiz_attempts_active_idx
  on public.quiz_attempts (user_id, quiz_set_id, status)
  where status = 'in_progress';

commit;
