-- Add subject-owned projects, project graphs, reusable document chunks, and graph provenance.
-- Chunk embeddings use all-MiniLM-L6-v2 (384 dimensions).

create schema if not exists extensions;
create extension if not exists vector with schema extensions;

create table public.projects (
  id uuid primary key default gen_random_uuid(),
  subject_id uuid not null references public.subjects (id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (subject_id, name),
  unique (id, subject_id)
);

create index projects_subject_id_idx on public.projects (subject_id);

create table public.graphs (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,
  subject_id uuid not null,
  name text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (project_id, subject_id)
    references public.projects (id, subject_id) on delete cascade,
  unique (project_id, name),
  unique (id, subject_id),
  unique (id, project_id)
);

create index graphs_project_id_idx on public.graphs (project_id);

-- A document can be reused by graphs in its subject. Membership is graph
-- scoped; a trigger below enforces that the selected document matches it.
create table public.graph_documents (
  graph_id uuid not null,
  document_id uuid not null,
  subject_id uuid not null,
  week text,
  sort_order integer not null default 0,
  status text not null default 'queued'
    check (status in ('queued', 'processing', 'complete', 'failed')),
  error text,
  added_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (graph_id, document_id),
  foreign key (graph_id, subject_id)
    references public.graphs (id, subject_id) on delete cascade,
  foreign key (document_id)
    references public.documents (id) on delete cascade
);

create index graph_documents_document_id_idx on public.graph_documents (document_id);

create table public.document_chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.documents (id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  content text not null,
  pages integer[] not null default '{}',
  content_hash text,
  embedding_model text not null default 'sentence-transformers/all-MiniLM-L6-v2',
  embedding extensions.vector(384),
  created_at timestamptz not null default now(),
  unique (document_id, chunk_index),
  unique (id, document_id)
);

create index document_chunks_document_id_idx on public.document_chunks (document_id);
create index document_chunks_embedding_hnsw_idx
  on public.document_chunks using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

create table public.concepts (
  id uuid primary key default gen_random_uuid(),
  graph_id uuid not null references public.graphs (id) on delete cascade,
  name text not null,
  normalized_name text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (graph_id, normalized_name),
  unique (id, graph_id)
);

create index concepts_graph_id_idx on public.concepts (graph_id);

create table public.relationships (
  id uuid primary key default gen_random_uuid(),
  graph_id uuid not null references public.graphs (id) on delete cascade,
  source_concept_id uuid not null,
  target_concept_id uuid not null,
  relationship_type text not null,
  summary text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (source_concept_id <> target_concept_id),
  foreign key (source_concept_id, graph_id)
    references public.concepts (id, graph_id) on delete cascade,
  foreign key (target_concept_id, graph_id)
    references public.concepts (id, graph_id) on delete cascade,
  unique (graph_id, source_concept_id, target_concept_id, relationship_type),
  unique (id, graph_id)
);

create index relationships_graph_id_idx on public.relationships (graph_id);
create index relationships_source_concept_idx on public.relationships (source_concept_id);
create index relationships_target_concept_idx on public.relationships (target_concept_id);

create table public.relationship_derivations (
  graph_id uuid not null,
  relationship_id uuid not null,
  premise_relationship_id uuid not null,
  derivation_rule text not null default 'unknown',
  created_at timestamptz not null default now(),
  check (relationship_id <> premise_relationship_id),
  foreign key (relationship_id, graph_id)
    references public.relationships (id, graph_id) on delete cascade,
  foreign key (premise_relationship_id, graph_id)
    references public.relationships (id, graph_id) on delete cascade,
  primary key (relationship_id, premise_relationship_id)
);

create index relationship_derivations_graph_idx
  on public.relationship_derivations (graph_id, relationship_id);

-- Provenance rows retain pre-fusion names/claims and point to the exact source
-- chunk. They can only reference documents attached to the same graph.
create table public.concept_provenance (
  id uuid primary key default gen_random_uuid(),
  graph_id uuid not null,
  concept_id uuid not null,
  document_id uuid not null,
  chunk_id uuid not null,
  source_name text not null,
  created_at timestamptz not null default now(),
  foreign key (concept_id, graph_id)
    references public.concepts (id, graph_id) on delete cascade,
  foreign key (graph_id, document_id)
    references public.graph_documents (graph_id, document_id) on delete cascade,
  foreign key (chunk_id, document_id)
    references public.document_chunks (id, document_id) on delete cascade,
  unique (concept_id, chunk_id, source_name)
);

create index concept_provenance_graph_concept_idx
  on public.concept_provenance (graph_id, concept_id);
create index concept_provenance_chunk_idx on public.concept_provenance (chunk_id);

create table public.relationship_provenance (
  id uuid primary key default gen_random_uuid(),
  graph_id uuid not null,
  relationship_id uuid not null,
  document_id uuid not null,
  chunk_id uuid not null,
  source_from text not null,
  source_to text not null,
  source_relationship_type text not null,
  summary text not null default '',
  context text not null default '',
  created_at timestamptz not null default now(),
  foreign key (relationship_id, graph_id)
    references public.relationships (id, graph_id) on delete cascade,
  foreign key (graph_id, document_id)
    references public.graph_documents (graph_id, document_id) on delete cascade,
  foreign key (chunk_id, document_id)
    references public.document_chunks (id, document_id) on delete cascade,
  unique (
    relationship_id,
    chunk_id,
    source_from,
    source_to,
    source_relationship_type
  )
);

create index relationship_provenance_graph_relationship_idx
  on public.relationship_provenance (graph_id, relationship_id);
create index relationship_provenance_chunk_idx
  on public.relationship_provenance (chunk_id);

-- Definition text is derived from current graph evidence and can be invalidated
-- without rerunning extraction or fusion.
create table public.concept_definitions (
  id uuid primary key default gen_random_uuid(),
  graph_id uuid not null,
  concept_id uuid not null,
  scope text not null check (scope in ('main', 'week')),
  week text,
  definition text not null,
  is_stale boolean not null default true,
  generated_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (id, graph_id),
  check (
    (scope = 'main' and week is null)
    or (scope = 'week' and week is not null)
  ),
  foreign key (concept_id, graph_id)
    references public.concepts (id, graph_id) on delete cascade
);

create unique index concept_definitions_main_unique_idx
  on public.concept_definitions (graph_id, concept_id)
  where scope = 'main';
create unique index concept_definitions_week_unique_idx
  on public.concept_definitions (graph_id, concept_id, week)
  where scope = 'week';
create index concept_definitions_graph_concept_idx
  on public.concept_definitions (graph_id, concept_id);

create table public.concept_definition_sources (
  definition_id uuid not null,
  graph_id uuid not null,
  document_id uuid not null,
  chunk_id uuid not null,
  created_at timestamptz not null default now(),
  primary key (definition_id, chunk_id),
  foreign key (definition_id, graph_id)
    references public.concept_definitions (id, graph_id) on delete cascade,
  foreign key (graph_id, document_id)
    references public.graph_documents (graph_id, document_id) on delete cascade,
  foreign key (chunk_id, document_id)
    references public.document_chunks (id, document_id) on delete cascade
);

create index concept_definition_sources_chunk_idx
  on public.concept_definition_sources (chunk_id);

create or replace function public.ensure_graph_document_same_subject()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if not exists (
    select 1
    from public.documents d
    where d.id = new.document_id
      and d.subject_id = new.subject_id
  ) then
    raise exception 'Graph documents must belong to the graph subject';
  end if;
  return new;
end;
$$;

create trigger graph_documents_same_subject
before insert or update of graph_id, document_id, subject_id
on public.graph_documents
for each row execute function public.ensure_graph_document_same_subject();

create trigger projects_set_updated_at
before update on public.projects
for each row execute function public.set_updated_at();

create trigger graphs_set_updated_at
before update on public.graphs
for each row execute function public.set_updated_at();

create trigger graph_documents_set_updated_at
before update on public.graph_documents
for each row execute function public.set_updated_at();

create trigger concepts_set_updated_at
before update on public.concepts
for each row execute function public.set_updated_at();

create trigger relationships_set_updated_at
before update on public.relationships
for each row execute function public.set_updated_at();

create trigger concept_definitions_set_updated_at
before update on public.concept_definitions
for each row execute function public.set_updated_at();

alter table public.projects enable row level security;
alter table public.graphs enable row level security;
alter table public.graph_documents enable row level security;
alter table public.document_chunks enable row level security;
alter table public.concepts enable row level security;
alter table public.relationships enable row level security;
alter table public.relationship_derivations enable row level security;
alter table public.concept_provenance enable row level security;
alter table public.relationship_provenance enable row level security;
alter table public.concept_definitions enable row level security;
alter table public.concept_definition_sources enable row level security;

create policy projects_own on public.projects
for all to authenticated
using (
  exists (
    select 1 from public.subjects s
    where s.id = projects.subject_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1 from public.subjects s
    where s.id = projects.subject_id and s.user_id = auth.uid()
  )
);

create policy graphs_own on public.graphs
for all to authenticated
using (
  exists (
    select 1
    from public.projects p
    join public.subjects s on s.id = p.subject_id
    where p.id = graphs.project_id
      and p.subject_id = graphs.subject_id
      and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.projects p
    join public.subjects s on s.id = p.subject_id
    where p.id = graphs.project_id
      and p.subject_id = graphs.subject_id
      and s.user_id = auth.uid()
  )
);

create policy graph_documents_own on public.graph_documents
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    join public.documents d on d.id = graph_documents.document_id
    where g.id = graph_documents.graph_id
      and g.subject_id = graph_documents.subject_id
      and d.subject_id = graph_documents.subject_id
      and d.user_id = auth.uid()
      and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    join public.documents d on d.id = graph_documents.document_id
    where g.id = graph_documents.graph_id
      and g.subject_id = graph_documents.subject_id
      and d.subject_id = graph_documents.subject_id
      and d.user_id = auth.uid()
      and s.user_id = auth.uid()
  )
);

create policy document_chunks_own on public.document_chunks
for all to authenticated
using (
  exists (
    select 1 from public.documents d
    where d.id = document_chunks.document_id and d.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1 from public.documents d
    where d.id = document_chunks.document_id and d.user_id = auth.uid()
  )
);

create policy concepts_own on public.concepts
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concepts.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concepts.graph_id and s.user_id = auth.uid()
  )
);

create policy relationships_own on public.relationships
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = relationships.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = relationships.graph_id and s.user_id = auth.uid()
  )
);

create policy relationship_derivations_own on public.relationship_derivations
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = relationship_derivations.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = relationship_derivations.graph_id and s.user_id = auth.uid()
  )
);

create policy concept_provenance_own on public.concept_provenance
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concept_provenance.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concept_provenance.graph_id and s.user_id = auth.uid()
  )
);

create policy relationship_provenance_own on public.relationship_provenance
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = relationship_provenance.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = relationship_provenance.graph_id and s.user_id = auth.uid()
  )
);

create policy concept_definitions_own on public.concept_definitions
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concept_definitions.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concept_definitions.graph_id and s.user_id = auth.uid()
  )
);

create policy concept_definition_sources_own on public.concept_definition_sources
for all to authenticated
using (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concept_definition_sources.graph_id and s.user_id = auth.uid()
  )
)
with check (
  exists (
    select 1
    from public.graphs g
    join public.projects p on p.id = g.project_id
    join public.subjects s on s.id = p.subject_id
    where g.id = concept_definition_sources.graph_id and s.user_id = auth.uid()
  )
);
