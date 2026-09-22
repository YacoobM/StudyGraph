-- Extend the existing users table with per-user subjects.

create extension if not exists pgcrypto;

create table if not exists public.subjects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now(),
  unique (user_id, name)
);

create index if not exists subjects_user_id_idx on public.subjects (user_id);

alter table public.subjects enable row level security;

create policy subjects_own on public.subjects
for all using (user_id = auth.uid())
with check (user_id = auth.uid());