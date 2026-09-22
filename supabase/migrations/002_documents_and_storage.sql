-- Store user-owned document metadata and files.

create table if not exists public.documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  subject_id uuid references public.subjects (id) on delete set null,
  filename text not null,
  storage_path text not null,
  size_bytes bigint not null default 0 check (size_bytes >= 0),
  mime text not null default 'application/octet-stream',
  status text not null default 'queued',
  week text,
  concepts integer not null default 0 check (concepts >= 0),
  relationships integer not null default 0 check (relationships >= 0),
  error text,
  uploaded_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, storage_path)
);

create index if not exists documents_user_id_idx on public.documents (user_id);
create index if not exists documents_subject_id_idx on public.documents (subject_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists documents_set_updated_at on public.documents;
create trigger documents_set_updated_at
before update on public.documents
for each row execute function public.set_updated_at();

alter table public.documents enable row level security;

create policy documents_own on public.documents
for all using (user_id = auth.uid())
with check (user_id = auth.uid());

insert into storage.buckets (id, name, public)
values ('studygraph-documents', 'studygraph-documents', false)
on conflict (id) do nothing;

create policy studygraph_documents_read on storage.objects
for select to authenticated
using (
  bucket_id = 'studygraph-documents'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy studygraph_documents_insert on storage.objects
for insert to authenticated
with check (
  bucket_id = 'studygraph-documents'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy studygraph_documents_update on storage.objects
for update to authenticated
using (
  bucket_id = 'studygraph-documents'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
)
with check (
  bucket_id = 'studygraph-documents'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy studygraph_documents_delete on storage.objects
for delete to authenticated
using (
  bucket_id = 'studygraph-documents'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);