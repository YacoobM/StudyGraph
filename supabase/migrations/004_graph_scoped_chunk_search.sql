-- Move the applied 003 schema to graph-owned documents and immutable graph versions.
begin;

do $$
begin
  if exists (
    select 1
    from public.graph_documents
    group by document_id
    having count(distinct graph_id) > 1
  ) then
    raise exception 'A document belongs to multiple graphs; resolve ownership before migrating';
  end if;

  if exists (
    select 1
    from public.documents d
    left join public.graph_documents gd on gd.document_id = d.id
    where gd.document_id is null
  ) then
    raise exception 'A document has no graph membership; assign it before migrating';
  end if;

  if exists (
    select 1
    from public.graphs g
    left join public.subjects s on s.id = g.subject_id
    where s.id is null
  ) then
    raise exception 'A graph has no owning user through its subject';
  end if;
end;
$$;

alter table public.graphs add column user_id uuid;
update public.graphs g
set user_id = s.user_id
from public.subjects s
where s.id = g.subject_id;
alter table public.graphs alter column user_id set not null;
alter table public.graphs
  add constraint graphs_user_id_fkey
  foreign key (user_id) references public.users (id) on delete cascade;

alter table public.documents add column graph_id uuid;
update public.documents d
set graph_id = gd.graph_id
from public.graph_documents gd
where gd.document_id = d.id;
alter table public.documents alter column graph_id set not null;
alter table public.documents
  add constraint documents_graph_id_fkey
  foreign key (graph_id) references public.graphs (id) on delete cascade;
alter table public.documents
  add constraint documents_graph_id_id_key unique (graph_id, id);

create table public.graph_versions (
  id uuid primary key default gen_random_uuid(),
  graph_id uuid not null references public.graphs (id) on delete cascade,
  version_number integer not null check (version_number > 0),
  created_at timestamptz not null default now(),
  is_sealed boolean not null default false,
  unique (graph_id, version_number),
  unique (id, graph_id)
);

insert into public.graph_versions (graph_id, version_number, created_at)
select id, 1, created_at
from public.graphs;

alter table public.concepts add column graph_version_id uuid;
update public.concepts c
set graph_version_id = v.id
from public.graph_versions v
where v.graph_id = c.graph_id;
alter table public.concepts alter column graph_version_id set not null;
alter table public.concepts
  add constraint concepts_version_graph_fkey
  foreign key (graph_version_id, graph_id)
  references public.graph_versions (id, graph_id) on delete cascade;
alter table public.concepts
  add constraint concepts_id_version_key unique (id, graph_version_id);
alter table public.concepts
  add column status text not null default 'active'
  check (status in ('active', 'needs_regeneration'));

alter table public.relationships add column graph_version_id uuid;
update public.relationships r
set graph_version_id = v.id
from public.graph_versions v
where v.graph_id = r.graph_id;
alter table public.relationships alter column graph_version_id set not null;
alter table public.relationships
  add constraint relationships_version_graph_fkey
  foreign key (graph_version_id, graph_id)
  references public.graph_versions (id, graph_id) on delete cascade;
alter table public.relationships
  add constraint relationships_id_version_key unique (id, graph_version_id);

alter table public.concept_provenance add column graph_version_id uuid;
update public.concept_provenance p
set graph_version_id = c.graph_version_id
from public.concepts c
where c.id = p.concept_id and c.graph_id = p.graph_id;
alter table public.concept_provenance alter column graph_version_id set not null;
alter table public.concept_provenance
  add constraint concept_provenance_version_fkey
  foreign key (concept_id, graph_version_id)
  references public.concepts (id, graph_version_id) on delete cascade;

alter table public.relationship_provenance add column graph_version_id uuid;
update public.relationship_provenance p
set graph_version_id = r.graph_version_id
from public.relationships r
where r.id = p.relationship_id and r.graph_id = p.graph_id;
alter table public.relationship_provenance alter column graph_version_id set not null;
alter table public.relationship_provenance
  add constraint relationship_provenance_version_fkey
  foreign key (relationship_id, graph_version_id)
  references public.relationships (id, graph_version_id) on delete cascade;

create index graph_versions_graph_id_idx on public.graph_versions (graph_id, version_number desc);
create index concepts_graph_version_id_idx on public.concepts (graph_version_id);
create index relationships_graph_version_id_idx on public.relationships (graph_version_id);
create index concept_provenance_version_concept_idx
  on public.concept_provenance (graph_version_id, concept_id);
create index relationship_provenance_version_relationship_idx
  on public.relationship_provenance (graph_version_id, relationship_id);

update public.graph_versions set is_sealed = true;

create or replace function public.search_graph_document_chunks(
  query_embedding extensions.vector(384),
  target_graph_id uuid,
  match_count integer default 3
)
returns table (
  chunk_id uuid,
  document_id uuid,
  chunk_index integer,
  filename text,
  content text,
  pages integer[],
  similarity double precision
)
language sql
stable
security invoker
set search_path = ''
as $$
  select
    chunks.id,
    chunks.document_id,
    chunks.chunk_index,
    documents.filename,
    chunks.content,
    chunks.pages,
    1 - (chunks.embedding OPERATOR(extensions.<=>) query_embedding) as similarity
  from public.document_chunks as chunks
  join public.documents as documents
    on documents.id = chunks.document_id
   and documents.graph_id = target_graph_id
  where chunks.embedding is not null
  order by chunks.embedding OPERATOR(extensions.<=>) query_embedding
  limit greatest(0, least(coalesce(match_count, 3), 20));
$$;

commit;