-- Historical graph versions are immutable, view-only JSONB snapshots.
-- Current documents/chunks remain separate for the latest active graph.
begin;

alter table public.graph_versions
  add column snapshot jsonb;

update public.graph_versions v
set snapshot = jsonb_build_object(
  'concepts', coalesce(
    (select jsonb_agg(jsonb_build_object(
      'id', c.id,
      'name', c.name,
      'normalized_name', c.normalized_name,
      'status', c.status
    ) order by c.normalized_name)
     from public.concepts c
     where c.graph_version_id = v.id), '[]'::jsonb),
  'relationships', coalesce(
    (select jsonb_agg(jsonb_build_object(
      'id', r.id,
      'source_concept_id', r.source_concept_id,
      'target_concept_id', r.target_concept_id,
      'relationship_type', r.relationship_type,
      'summary', r.summary
    ) order by r.relationship_type, r.id)
     from public.relationships r
     where r.graph_version_id = v.id), '[]'::jsonb)
)
where snapshot is null;

alter table public.graph_versions
  alter column snapshot set default '{"concepts": [], "relationships": []}'::jsonb,
  alter column snapshot set not null;

-- Current working support is separate from historical snapshots. It is used
-- only to reconcile document deletion without querying old versions.
alter table public.graphs
  add column active_support jsonb not null
  default '{"concepts": {}, "relationships": {}}'::jsonb;

drop table if exists public.relationship_provenance cascade;
drop table if exists public.concept_provenance cascade;
drop table if exists public.relationships cascade;
drop table if exists public.concepts cascade;

commit;