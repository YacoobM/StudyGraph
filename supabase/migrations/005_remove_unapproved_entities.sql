-- Finish the graph-owned, versioned model introduced in migration 004.
begin;

drop policy if exists projects_own on public.projects;
drop policy if exists graphs_own on public.graphs;
drop policy if exists graph_documents_own on public.graph_documents;
drop policy if exists concepts_own on public.concepts;
drop policy if exists relationships_own on public.relationships;
drop policy if exists relationship_derivations_own on public.relationship_derivations;
drop policy if exists concept_provenance_own on public.concept_provenance;
drop policy if exists relationship_provenance_own on public.relationship_provenance;
drop policy if exists concept_definitions_own on public.concept_definitions;
drop policy if exists concept_definition_sources_own on public.concept_definition_sources;
drop policy if exists documents_own on public.documents;
drop policy if exists document_chunks_own on public.document_chunks;

drop table if exists public.relationship_derivations cascade;
drop table if exists public.concept_definitions cascade;
drop table if exists public.concept_definition_sources cascade;
drop table if exists public.graph_documents cascade;
drop table if exists public.projects cascade;
drop function if exists public.ensure_graph_document_same_subject();

do $$
declare
  item record;
begin
  for item in
    select conrelid::regclass as table_name, conname
    from pg_constraint
    where conrelid in (
      'public.concept_provenance'::regclass,
      'public.relationship_provenance'::regclass
    )
      and contype = 'f'
      and pg_get_constraintdef(oid) like '%graph_id%'
  loop
    execute format(
      'alter table %s drop constraint %I',
      item.table_name,
      item.conname
    );
  end loop;

  for item in
    select conname
    from pg_constraint
    where conrelid = 'public.relationships'::regclass
      and contype in ('f', 'u')
      and pg_get_constraintdef(oid) like '%graph_id%'
  loop
    execute format('alter table public.relationships drop constraint %I', item.conname);
  end loop;

  for item in
    select conname
    from pg_constraint
    where conrelid = 'public.concepts'::regclass
      and contype in ('f', 'u')
      and pg_get_constraintdef(oid) like '%graph_id%'
  loop
    execute format('alter table public.concepts drop constraint %I', item.conname);
  end loop;
end;
$$;

alter table public.concepts drop column graph_id;
alter table public.concepts
  add constraint concepts_graph_version_id_fkey
  foreign key (graph_version_id) references public.graph_versions (id) on delete cascade;
alter table public.concepts
  add constraint concepts_graph_version_name_key unique (graph_version_id, normalized_name);

alter table public.relationships drop column graph_id;
alter table public.relationships
  add constraint relationships_graph_version_id_fkey
  foreign key (graph_version_id) references public.graph_versions (id) on delete cascade;
alter table public.relationships
  add constraint relationships_source_version_fkey
  foreign key (source_concept_id, graph_version_id)
  references public.concepts (id, graph_version_id) on delete cascade;
alter table public.relationships
  add constraint relationships_target_version_fkey
  foreign key (target_concept_id, graph_version_id)
  references public.concepts (id, graph_version_id) on delete cascade;
alter table public.relationships
  add constraint relationships_graph_version_edge_key
  unique (graph_version_id, source_concept_id, target_concept_id, relationship_type);

alter table public.concept_provenance
  drop constraint if exists concept_provenance_graph_id_document_id_fkey;
alter table public.concept_provenance
  drop constraint if exists concept_provenance_chunk_id_document_id_fkey;
alter table public.concept_provenance drop column graph_id;

alter table public.relationship_provenance
  drop constraint if exists relationship_provenance_graph_id_document_id_fkey;
alter table public.relationship_provenance
  drop constraint if exists relationship_provenance_chunk_id_document_id_fkey;
alter table public.relationship_provenance drop column graph_id;

drop index if exists public.concepts_graph_id_idx;
drop index if exists public.relationships_graph_id_idx;
drop index if exists public.concept_provenance_graph_concept_idx;
drop index if exists public.relationship_provenance_graph_relationship_idx;
create index if not exists concepts_graph_version_id_idx on public.concepts (graph_version_id);
create index if not exists relationships_graph_version_id_idx on public.relationships (graph_version_id);

alter table public.graphs drop column project_id cascade;
alter table public.graphs drop column subject_id cascade;
alter table public.documents drop column subject_id cascade;
alter table public.documents drop column user_id cascade;
alter table public.documents
  add constraint documents_graph_storage_path_key unique (graph_id, storage_path);
create index if not exists documents_graph_id_idx on public.documents (graph_id);

alter table public.graph_versions enable row level security;

create or replace function public.guard_graph_version_changes()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if tg_op = 'INSERT' then
    if new.is_sealed then
      raise exception 'Create a graph version unsealed, add its rows, then seal it';
    end if;
    return new;
  end if;

  if tg_op = 'UPDATE' then
    if old.is_sealed
      or new.is_sealed is distinct from true
      or (to_jsonb(new) - 'is_sealed') is distinct from (to_jsonb(old) - 'is_sealed') then
      raise exception 'Graph versions are immutable; create a new version instead';
    end if;
    return new;
  end if;

  if pg_trigger_depth() > 1 then
    return old;
  end if;
  raise exception 'Graph versions are immutable; create a new version instead';
end;
$$;

create or replace function public.guard_sealed_graph_version_rows()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  version_is_sealed boolean;
begin
  if tg_op = 'UPDATE' then
    select exists (
      select 1 from public.graph_versions v
      where v.id = old.graph_version_id and v.is_sealed
    ) or exists (
      select 1 from public.graph_versions v
      where v.id = new.graph_version_id and v.is_sealed
    ) into version_is_sealed;
  elsif tg_op = 'DELETE' then
    select is_sealed into version_is_sealed
    from public.graph_versions where id = old.graph_version_id;
  else
    select is_sealed into version_is_sealed
    from public.graph_versions where id = new.graph_version_id;
  end if;

  if coalesce(version_is_sealed, false) then
    raise exception 'Rows in a sealed graph version are immutable';
  end if;

  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger graph_versions_immutable
before insert or update or delete on public.graph_versions
for each row execute function public.guard_graph_version_changes();

create trigger concepts_version_immutable
before insert or update or delete on public.concepts
for each row execute function public.guard_sealed_graph_version_rows();

create trigger relationships_version_immutable
before insert or update or delete on public.relationships
for each row execute function public.guard_sealed_graph_version_rows();

create trigger concept_provenance_version_immutable
before insert or update or delete on public.concept_provenance
for each row execute function public.guard_sealed_graph_version_rows();

create trigger relationship_provenance_version_immutable
before insert or update or delete on public.relationship_provenance
for each row execute function public.guard_sealed_graph_version_rows();

create policy graphs_own on public.graphs
for all to authenticated
using (user_id = auth.uid())
with check (user_id = auth.uid());

create policy documents_own on public.documents
for all to authenticated
using (
  exists (
    select 1 from public.graphs g
    where g.id = documents.graph_id and g.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1 from public.graphs g
    where g.id = documents.graph_id and g.user_id = auth.uid()
  )
);

create policy document_chunks_own on public.document_chunks
for all to authenticated
using (
  exists (
    select 1
    from public.documents d
    join public.graphs g on g.id = d.graph_id
    where d.id = document_chunks.document_id and g.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.documents d
    join public.graphs g on g.id = d.graph_id
    where d.id = document_chunks.document_id and g.user_id = auth.uid()
  )
);

create policy graph_versions_read on public.graph_versions
for select to authenticated
using (
  exists (
    select 1 from public.graphs g
    where g.id = graph_versions.graph_id and g.user_id = auth.uid()
  )
);

create policy graph_versions_insert on public.graph_versions
for insert to authenticated
with check (
  not is_sealed and exists (
    select 1 from public.graphs g
    where g.id = graph_versions.graph_id and g.user_id = auth.uid()
  )
);

create policy graph_versions_seal on public.graph_versions
for update to authenticated
using (not is_sealed)
with check (is_sealed);

create policy concepts_read on public.concepts
for select to authenticated
using (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = concepts.graph_version_id and g.user_id = auth.uid()
  )
);

create policy concepts_insert on public.concepts
for insert to authenticated
with check (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = concepts.graph_version_id and g.user_id = auth.uid()
  )
);

create policy relationships_read on public.relationships
for select to authenticated
using (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = relationships.graph_version_id and g.user_id = auth.uid()
  )
);

create policy relationships_insert on public.relationships
for insert to authenticated
with check (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = relationships.graph_version_id and g.user_id = auth.uid()
  )
);

create policy concept_provenance_read on public.concept_provenance
for select to authenticated
using (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = concept_provenance.graph_version_id and g.user_id = auth.uid()
  )
);

create policy concept_provenance_insert on public.concept_provenance
for insert to authenticated
with check (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = concept_provenance.graph_version_id and g.user_id = auth.uid()
  )
);

create policy relationship_provenance_read on public.relationship_provenance
for select to authenticated
using (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = relationship_provenance.graph_version_id and g.user_id = auth.uid()
  )
);

create policy relationship_provenance_insert on public.relationship_provenance
for insert to authenticated
with check (
  exists (
    select 1
    from public.graph_versions v
    join public.graphs g on g.id = v.graph_id
    where v.id = relationship_provenance.graph_version_id and g.user_id = auth.uid()
  )
);

commit;
