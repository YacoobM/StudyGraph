-- Preserve the evolution week at the moment provenance is recorded.
begin;

alter table public.concept_provenance
  add column source_week integer not null default 1
  check (source_week > 0);

alter table public.relationship_provenance
  add column source_week integer not null default 1
  check (source_week > 0);

commit;
