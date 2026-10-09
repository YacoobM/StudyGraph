-- Restore structured, version-scoped graph entities after migration 006.
-- JSON snapshots remain the immutable render cache; these rows are the
-- queryable source of truth for provenance and concept evolution.
-- Provenance keeps denormalized source metadata so it survives document
-- deletion. There are intentionally no foreign keys to documents/chunks.
begin;

create table public.concepts (
  id uuid primary key,
  graph_version_id uuid not null references public.graph_versions (id) on delete cascade,
  name text not null,
  normalized_name text not null,
  status text not null default 'active'
    check (status in ('active', 'needs_regeneration')),
  unique (graph_version_id, normalized_name),
  unique (id, graph_version_id)
);

create index concepts_graph_version_id_idx
  on public.concepts (graph_version_id);

create table public.relationships (
  id uuid primary key,
  graph_version_id uuid not null references public.graph_versions (id) on delete cascade,
  source_concept_id uuid not null,
  target_concept_id uuid not null,
  relationship_type text not null,
  summary text not null default '',
  foreign key (source_concept_id, graph_version_id)
    references public.concepts (id, graph_version_id) on delete cascade,
  foreign key (target_concept_id, graph_version_id)
    references public.concepts (id, graph_version_id) on delete cascade,
  unique (id, graph_version_id),
  unique (graph_version_id, source_concept_id, target_concept_id, relationship_type)
);

create index relationships_graph_version_id_idx
  on public.relationships (graph_version_id);

create table public.concept_provenance (
  id uuid primary key default gen_random_uuid(),
  graph_version_id uuid not null,
  concept_id uuid not null,
  document_id uuid,
  document_filename text not null,
  chunk_id uuid,
  source_name text not null default '',
  source_excerpt text not null default '',
  page_start integer,
  page_end integer,
  created_at timestamptz not null default now(),
  foreign key (concept_id, graph_version_id)
    references public.concepts (id, graph_version_id) on delete cascade,
  unique (graph_version_id, concept_id, document_id, chunk_id, source_name)
);

create index concept_provenance_version_concept_idx
  on public.concept_provenance (graph_version_id, concept_id);

create table public.relationship_provenance (
  id uuid primary key default gen_random_uuid(),
  graph_version_id uuid not null,
  relationship_id uuid not null,
  document_id uuid,
  document_filename text not null,
  chunk_id uuid,
  source_from text not null default '',
  source_to text not null default '',
  source_relationship_type text not null default '',
  summary text not null default '',
  context text not null default '',
  created_at timestamptz not null default now(),
  foreign key (relationship_id, graph_version_id)
    references public.relationships (id, graph_version_id) on delete cascade,
  unique (graph_version_id, relationship_id, document_id, chunk_id,
          source_from, source_to, source_relationship_type)
);

create index relationship_provenance_version_relationship_idx
  on public.relationship_provenance (graph_version_id, relationship_id);

-- Rehydrate graph entities from snapshots. Exact historic chunk evidence was
-- removed by migration 006 and cannot be reconstructed here.
insert into public.concepts (id, graph_version_id, name, normalized_name, status)
select c.id, v.id, c.name, c.normalized_name, coalesce(c.status, 'active')
from public.graph_versions v
cross join lateral jsonb_to_recordset(v.snapshot->'concepts') as c(
  id uuid, name text, normalized_name text, status text
)
on conflict (id) do nothing;

insert into public.relationships (
  id, graph_version_id, source_concept_id, target_concept_id,
  relationship_type, summary
)
select r.id, v.id, r.source_concept_id, r.target_concept_id,
       r.relationship_type, coalesce(r.summary, '')
from public.graph_versions v
cross join lateral jsonb_to_recordset(v.snapshot->'relationships') as r(
  id uuid, source_concept_id uuid, target_concept_id uuid,
  relationship_type text, summary text
)
where exists (
  select 1 from public.concepts c
  where c.id = r.source_concept_id and c.graph_version_id = v.id
)
and exists (
  select 1 from public.concepts c
  where c.id = r.target_concept_id and c.graph_version_id = v.id
)
on conflict (id) do nothing;

create or replace function public.guard_restored_version_rows()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  version_is_sealed boolean;
begin
  if tg_op = 'DELETE' then
    select is_sealed into version_is_sealed
    from public.graph_versions where id = old.graph_version_id;
  else
    select is_sealed into version_is_sealed
    from public.graph_versions where id = new.graph_version_id;
  end if;
  if coalesce(version_is_sealed, false) then
    raise exception 'Rows in a sealed graph version are immutable';
  end if;
  return case when tg_op = 'DELETE' then old else new end;
end;
$$;

create trigger concepts_restored_version_immutable
before insert or update or delete on public.concepts
for each row execute function public.guard_restored_version_rows();
create trigger relationships_restored_version_immutable
before insert or update or delete on public.relationships
for each row execute function public.guard_restored_version_rows();
create trigger concept_provenance_restored_version_immutable
before insert or update or delete on public.concept_provenance
for each row execute function public.guard_restored_version_rows();
create trigger relationship_provenance_restored_version_immutable
before insert or update or delete on public.relationship_provenance
for each row execute function public.guard_restored_version_rows();

alter table public.concepts enable row level security;
alter table public.relationships enable row level security;
alter table public.concept_provenance enable row level security;
alter table public.relationship_provenance enable row level security;

create policy concepts_read on public.concepts
for select to authenticated using (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = concepts.graph_version_id and g.user_id = auth.uid())
);

create policy concepts_insert on public.concepts
for insert to authenticated with check (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = concepts.graph_version_id
            and not v.is_sealed and g.user_id = auth.uid())
);

create policy relationships_read on public.relationships
for select to authenticated using (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = relationships.graph_version_id and g.user_id = auth.uid())
);

create policy relationships_insert on public.relationships
for insert to authenticated with check (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = relationships.graph_version_id
            and not v.is_sealed and g.user_id = auth.uid())
);

create policy concept_provenance_read on public.concept_provenance
for select to authenticated using (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = concept_provenance.graph_version_id and g.user_id = auth.uid())
);

create policy concept_provenance_insert on public.concept_provenance
for insert to authenticated with check (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = concept_provenance.graph_version_id
            and not v.is_sealed and g.user_id = auth.uid())
);

create policy relationship_provenance_read on public.relationship_provenance
for select to authenticated using (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = relationship_provenance.graph_version_id and g.user_id = auth.uid())
);

create policy relationship_provenance_insert on public.relationship_provenance
for insert to authenticated with check (
  exists (select 1 from public.graph_versions v
          join public.graphs g on g.id = v.graph_id
          where v.id = relationship_provenance.graph_version_id
            and not v.is_sealed and g.user_id = auth.uid())
);

commit;
