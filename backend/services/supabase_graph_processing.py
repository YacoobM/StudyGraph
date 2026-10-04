from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from uuid import uuid4

import numpy as np
from fastapi import HTTPException

from services.document_service import make_extraction_batches, extract_with_exp_backoff
from app import state as runtime_state
from repositories.supabase_documents import delete_graph_document_data, list_graph_documents, set_graph_document_status
from repositories.supabase_graphs import (
	create_graph,
	get_graph_active_support,
	list_graph_versions,
	load_graph_version,
	request_table,
	set_graph_active_support,
)
from repositories.users import SupabaseIdentity
from services import graph_fusion
from services.embeddings import EMBEDDING_DIMENSIONS
from services.graph_engine import StudyGraphEngine
from services.evolution_data import generate_concept_definitions_batch


logger = logging.getLogger(__name__)
PERSIST_BATCH_SIZE = 500


def _embedding_array(value):
	if isinstance(value, str):
		vector = np.fromstring(value.strip().strip("[]"), sep=",")
	else:
		vector = np.asarray(value, dtype=np.float32)
	if vector.size != EMBEDDING_DIMENSIONS:
		raise HTTPException(status_code=502, detail="Stored chunk has an invalid embedding.")
	return vector


def _read_document_chunks(identity: SupabaseIdentity, document_id: str) -> list[dict]:
	return request_table(
		identity,
		"document_chunks",
		f"?select=id,document_id,chunk_index,content,pages,embedding&document_id=eq.{document_id}&order=chunk_index.asc",
	)


def _write_rows(identity: SupabaseIdentity, table: str, rows: list[dict], select: str) -> list[dict]:
	result = []
	for offset in range(0, len(rows), PERSIST_BATCH_SIZE):
		result.extend(request_table(
			identity,
			table,
			f"?select={select}",
			method="POST",
			payload=rows[offset:offset + PERSIST_BATCH_SIZE],
			prefer="return=representation",
		))
	return result


def _canonical_name(engine: StudyGraphEngine, name: str) -> str:
	normalized = graph_fusion.normalize_concept(name)
	canonical = engine.canonical_nodes.get(name, engine.canonical_nodes.get(normalized, normalized))
	return graph_fusion.normalize_concept(canonical)


def _restore_engine(
	identity: SupabaseIdentity,
	graph_id: str,
	engine: StudyGraphEngine,
	documents: list[dict],
	chunks_by_document: dict[str, list[dict]],
	latest_snapshot: dict | None,
):
	documents_by_id = {str(document["id"]): document for document in documents}
	chunks_by_id = {
		str(chunk["id"]): chunk
		for document_chunks in chunks_by_document.values()
		for chunk in document_chunks
	}
	for document_id, chunks in chunks_by_document.items():
		document = documents_by_id[document_id]
		week = document.get("week") or "Week 1"
		engine.uploads_by_week[f"{week}:{document_id}"] = document["filename"]
		for chunk in chunks:
			entry = {
				"chunk_id": str(chunk["id"]),
				"text": chunk["content"],
				"pages": chunk.get("pages") or [],
				"doc_name": document_id,
				"embedding": _embedding_array(chunk["embedding"]),
			}
			engine.chunks_by_week[week].append(entry)
			engine.chunks_by_id[entry["chunk_id"]] = {
				**entry,
				"source": document_id,
				"week": week,
			}

	concept_support = []
	relationship_support = []
	previous_status = {}
	if not latest_snapshot:
		return concept_support, relationship_support, previous_status

	concepts_by_id = {str(concept["id"]): concept for concept in latest_snapshot["concepts"]}
	for concept in latest_snapshot["concepts"]:
		previous_status[graph_fusion.normalize_concept(concept["name"])] = concept.get("status", "active")

	grouped_concepts = {}
	for provenance in latest_snapshot["concept_provenance"]:
		document_id = str(provenance["document_id"])
		chunk_id = str(provenance["chunk_id"])
		document = documents_by_id.get(document_id)
		chunk = chunks_by_id.get(chunk_id)
		if not document or not chunk:
			continue
		raw_name = str(provenance["source_name"]).lower().strip()
		if not raw_name:
			continue
		concept_row = concepts_by_id.get(str(provenance["concept_id"]))
		if not concept_row:
			continue
		group_key = (raw_name, document_id, document.get("week") or "Week 1")
		grouped_concepts.setdefault(group_key, set()).update(chunk.get("pages") or [])
		concept_support.append({
			"source_name": raw_name,
			"document_id": document_id,
			"chunk_id": chunk_id,
			"source_week": int(str(document.get("week") or "Week 1").split()[-1]),
		})
	for (name, document_id, week), pages in grouped_concepts.items():
		engine.concepts.append({
			"name": name,
			"source": document_id,
			"week": week,
			"pages": sorted(pages),
			"description": concept_row.get("description") or "",
		})
		engine._doc_concept_names.setdefault(document_id, set()).add(name)
		engine.known_concepts.add(name)

	for provenance in latest_snapshot["relationship_provenance"]:
		document_id = str(provenance["document_id"])
		chunk_id = str(provenance["chunk_id"])
		if document_id not in documents_by_id or chunk_id not in chunks_by_id:
			continue
		source_from = str(provenance["source_from"]).lower().strip()
		source_to = str(provenance["source_to"]).lower().strip()
		relation = str(provenance["source_relationship_type"]).lower().strip()
		engine.edges.append({
			"from": source_from,
			"to": source_to,
			"relation": relation,
			"source": document_id,
			"summary": provenance.get("summary") or "",
			"context": provenance.get("context") or "",
			"chunk_ids": [chunk_id],
		})
		relationship_support.append({
			"source_from": source_from,
			"source_to": source_to,
			"source_relationship_type": relation,
			"document_id": document_id,
			"chunk_id": chunk_id,
			"summary": provenance.get("summary") or "",
			"context": provenance.get("context") or "",
			"source_week": int(str((documents_by_id.get(document_id) or {}).get("week") or "Week 1").split()[-1]),
		})

	engine.rebuild_known_concept_embeddings()
	return concept_support, relationship_support, previous_status


def _persist_version(
	identity: SupabaseIdentity,
	graph_id: str,
	version_number: int,
	engine: StudyGraphEngine,
	concept_support: list[dict],
	relationship_support: list[dict],
	previous_status: dict[str, str],
	documents_by_id: dict[str, dict],
	concept_descriptions: dict[str, str] | None = None,
):
	concept_descriptions = concept_descriptions or {}
	canonical_names = set()
	for concept in engine.concepts:
		canonical_names.add(_canonical_name(engine, concept["name"]))
	for edge in engine.fused_edges:
		canonical_names.add(_canonical_name(engine, edge["from"]))
		canonical_names.add(_canonical_name(engine, edge["to"]))
	canonical_names.discard("")

	concepts = [
		{
			"id": str(uuid4()),
			"name": name,
			"normalized_name": graph_fusion.normalize_concept(name),
			"status": previous_status.get(graph_fusion.normalize_concept(name), "active"),
			**({"description": concept_descriptions[graph_fusion.normalize_concept(name)]}
			   if concept_descriptions.get(graph_fusion.normalize_concept(name)) else {}),
		}
		for name in sorted(canonical_names)
	]
	concept_id_by_name = {
		graph_fusion.normalize_concept(row["name"]): row["id"]
		for row in concepts
	}
	relationships = []
	edges = engine.fused_edges
	for edge in edges:
		source_id = concept_id_by_name.get(_canonical_name(engine, edge["from"]))
		target_id = concept_id_by_name.get(_canonical_name(engine, edge["to"]))
		if not source_id or not target_id or source_id == target_id:
			continue
		relationships.append({
			"id": str(uuid4()),
			"source_concept_id": source_id,
			"target_concept_id": target_id,
			"relationship_type": str(edge.get("relation") or "related_to").lower().strip(),
			"summary": edge.get("summary") or "",
		})
	snapshot = {"concepts": concepts, "relationships": relationships}
	version_rows = request_table(
		identity,
		"graph_versions",
		"?select=id,graph_id,version_number,created_at,is_sealed,snapshot",
		method="POST",
		payload={"graph_id": graph_id, "version_number": version_number, "is_sealed": False, "snapshot": snapshot},
		prefer="return=representation",
	)
	if len(version_rows) != 1:
		raise HTTPException(status_code=502, detail="Supabase did not return the new graph version.")
	version_id = str(version_rows[0]["id"])
	request_table(
		identity,
		"concepts",
		"?select=id",
		method="POST",
		payload=[
			{
				"id": concept["id"],
				"name": concept["name"],
				"normalized_name": concept["normalized_name"],
				"status": concept["status"],
				"graph_version_id": version_id,
			}
			for concept in concepts
		],
		prefer="return=minimal",
	)
	request_table(
		identity,
		"relationships",
		"?select=id",
		method="POST",
		payload=[
			{
				**relationship,
				"graph_version_id": version_id,
			}
			for relationship in relationships
		],
		prefer="return=minimal",
	)

	concept_ids = {
		graph_fusion.normalize_concept(concept["name"]): concept["id"]
		for concept in concepts
	}
	concept_provenance = []
	for support in concept_support:
		concept_id = concept_ids.get(
			graph_fusion.normalize_concept(support.get("source_name", ""))
		)
		document_id = str(support.get("document_id") or "")
		if not concept_id or not document_id or document_id not in documents_by_id:
			continue
		concept_provenance.append({
			"graph_version_id": version_id,
			"concept_id": concept_id,
			"document_id": document_id,
			"document_filename": documents_by_id[document_id]["filename"],
			"chunk_id": support.get("chunk_id") or None,
			"source_name": support.get("source_name") or "",
			"source_week": int(support.get("source_week") or version_number),
		})
	if concept_provenance:
		request_table(
			identity,
			"concept_provenance",
			"?select=id",
			method="POST",
			payload=list({
				(row["concept_id"], row["document_id"], row["chunk_id"], row["source_name"]): row
				for row in concept_provenance
			}.values()),
			prefer="return=minimal",
		)

	concept_names_by_id = {
		concept["id"]: graph_fusion.normalize_concept(concept["name"])
		for concept in concepts
	}
	relationship_ids = {
		(
			concept_names_by_id.get(edge["source_concept_id"], ""),
			str(edge["relationship_type"]).lower().strip(),
			concept_names_by_id.get(edge["target_concept_id"], ""),
		): edge["id"]
		for edge in relationships
	}
	relationship_provenance = []
	for support in relationship_support:
		document_id = str(support.get("document_id") or "")
		key = (
			graph_fusion.normalize_concept(support.get("source_from", "")),
			str(support.get("source_relationship_type") or "related_to").lower().strip(),
			graph_fusion.normalize_concept(support.get("source_to", "")),
		)
		relationship_id = relationship_ids.get(key)
		if not relationship_id or document_id not in documents_by_id:
			continue
		relationship_provenance.append({
			"graph_version_id": version_id,
			"relationship_id": relationship_id,
			"document_id": document_id,
			"document_filename": documents_by_id[document_id]["filename"],
			"chunk_id": support.get("chunk_id") or None,
			"source_from": support.get("source_from") or "",
			"source_to": support.get("source_to") or "",
			"source_relationship_type": support.get("source_relationship_type") or "related_to",
			"summary": support.get("summary") or "",
			"context": support.get("context") or "",
			"source_week": int(support.get("source_week") or version_number),
		})
	if relationship_provenance:
		request_table(
			identity,
			"relationship_provenance",
			"?select=id",
			method="POST",
			payload=list({
				(row["relationship_id"], row["document_id"], row["chunk_id"], row["source_from"], row["source_to"], row["source_relationship_type"]): row
				for row in relationship_provenance
			}.values()),
			prefer="return=minimal",
		)
	request_table(
		identity,
		"graph_versions",
		f"?id=eq.{version_id}&is_sealed=eq.false",
		method="PATCH",
		payload={"is_sealed": True},
		prefer="return=minimal",
	)
	active_support = {"concepts": defaultdict(set), "relationships": defaultdict(set)}
	for support in concept_support:
		active_support["concepts"][_canonical_name(engine, support["source_name"])].add(str(support["document_id"]))
	for support in relationship_support:
		if not support.get("document_id"):
			continue
		key = (
			f"{_canonical_name(engine, support['source_from'])}|"
			f"{str(support['source_relationship_type']).lower().strip()}|"
			f"{_canonical_name(engine, support['source_to'])}"
		)
		active_support["relationships"][key].add(str(support["document_id"]))
	active_support = {
		section: {key: sorted(values) for key, values in values.items()}
		for section, values in active_support.items()
	}
	set_graph_active_support(identity, graph_id, active_support)
	return {**version_rows[0], "snapshot": snapshot, "is_sealed": True}


def _build_deleted_snapshot(snapshot: dict, active_support: dict, document_id: str) -> tuple[dict, dict]:
	# A surviving concept can still have enough support to remain in the graph,
	# while its stored definition/evidence has changed because the deleted
	# document contributed to it. Keep that distinction explicit for the UI.
	affected_concept_ids = {
		str(row.get("concept_id"))
		for row in (snapshot.get("concept_provenance") or [])
		if str(row.get("document_id")) == str(document_id) and row.get("concept_id")
	}
	concept_support = {
		key: [str(value) for value in values if str(value) != document_id]
		for key, values in (active_support.get("concepts") or {}).items()
	}
	deleted_concepts = {
		key for key, values in concept_support.items() if not values
	}
	concept_support = {
		key: values for key, values in concept_support.items() if values
	}
	concepts = []
	for concept in snapshot.get("concepts", []):
		key = str(concept.get("normalized_name") or graph_fusion.normalize_concept(concept.get("name", "")))
		if key in deleted_concepts:
			continue
		updated = dict(concept)
		# The remaining provenance is authoritative. A concept that still has
		# support stays in the graph. If deleted evidence contributed to its
		# definition, retain a targeted stale/degraded marker for later refresh.
		if str(concept.get("id")) in affected_concept_ids:
			updated["status"] = "needs_regeneration"
		else:
			updated["status"] = "active"
		concepts.append(updated)

	concept_ids = {str(concept["id"]): str(concept.get("normalized_name") or graph_fusion.normalize_concept(concept.get("name", ""))) for concept in concepts}
	relationship_support = {}
	for key, values in (active_support.get("relationships") or {}).items():
		remaining = [str(value) for value in values if str(value) != document_id]
		if remaining:
			relationship_support[key] = remaining

	relationships = []
	for relationship in snapshot.get("relationships", []):
		if str(relationship.get("source_concept_id")) not in concept_ids or str(relationship.get("target_concept_id")) not in concept_ids:
			continue
		source = concept_ids[str(relationship["source_concept_id"])]
		target = concept_ids[str(relationship["target_concept_id"])]
		relation = str(relationship.get("relationship_type") or "related_to").lower().strip()
		key = f"{source}|{relation}|{target}"
		if document_id in [str(value) for value in (active_support.get("relationships") or {}).get(key, [])]:
			continue
		relationships.append(relationship)
	concept_ids = {str(concept["id"]) for concept in concepts}
	relationship_ids = {str(relationship["id"]) for relationship in relationships}
	deleted_id = str(document_id)
	concept_provenance = [
		{key: value for key, value in row.items() if key not in {"id", "graph_version_id"}}
		for row in (snapshot.get("concept_provenance") or [])
		if str(row.get("document_id")) != deleted_id
		and str(row.get("concept_id")) in concept_ids
	]
	relationship_provenance = [
		{key: value for key, value in row.items() if key not in {"id", "graph_version_id"}}
		for row in (snapshot.get("relationship_provenance") or [])
		if str(row.get("document_id")) != deleted_id
		and str(row.get("relationship_id")) in relationship_ids
	]
	return {
		"concepts": concepts,
		"relationships": relationships,
		"concept_provenance": concept_provenance,
		"relationship_provenance": relationship_provenance,
	}, {"concepts": concept_support, "relationships": relationship_support}


async def _generate_concept_descriptions(client, concepts, concept_support, chunks_by_document, engine):
	"""Generate grounded descriptions for concepts in a newly created version."""
	if not client:
		return {}
	by_name = {}
	for support in concept_support:
		name = graph_fusion.normalize_concept(support.get("source_name", ""))
		if not name:
			continue
		chunk_id = str(support.get("chunk_id") or "")
		chunk = next(
			(row for row in chunks_by_document.get(str(support.get("document_id")), [])
			 if str(row.get("id")) == chunk_id),
			 None,
		)
		if chunk:
			by_name.setdefault(name, []).append(chunk.get("content", "")[:700])
	requests = []
	for concept in concepts:
		name = graph_fusion.normalize_concept(concept["name"])
		evidence = "\n\n".join(dict.fromkeys(by_name.get(name, [])))
		if evidence:
			requests.append({
				"key": name,
				"concept_name": concept["name"],
				"week": "Current material",
				"evidence": evidence[:2400],
			})
	if not requests:
		return {}
	try:
		return await generate_concept_definitions_batch(client, requests)
	except Exception as error:
		logger.warning("concept_definition_generation_failed error=%s", error)
		return {}


async def delete_graph_document_and_create_version(
	identity: SupabaseIdentity,
	graph_id: str,
	document_id: str,
) -> dict:
	documents = list_graph_documents(identity, graph_id)
	document = next((row for row in documents if str(row["id"]) == document_id), None)
	if document is None:
		raise HTTPException(status_code=404, detail="Document not found in this graph.")

	versions = request_table(
		identity,
		"graph_versions",
		f"?select=id,version_number,is_sealed&graph_id=eq.{graph_id}&is_sealed=eq.true&order=version_number.desc",
	)
	if not versions:
		raise HTTPException(status_code=409, detail="Cannot delete from a graph with no sealed version.")
	latest = load_graph_version(identity, graph_id, str(versions[0]["id"]))
	active_support = get_graph_active_support(identity, graph_id)
	snapshot, next_support = _build_deleted_snapshot(latest, active_support, document_id)
	version_number = max(int(row["version_number"]) for row in versions) + 1
	created = request_table(
		identity,
		"graph_versions",
		"?select=id,graph_id,version_number,created_at,is_sealed,snapshot",
		method="POST",
		payload={"graph_id": graph_id, "version_number": version_number, "is_sealed": False, "snapshot": snapshot},
		prefer="return=representation",
	)
	if len(created) != 1:
		raise HTTPException(status_code=502, detail="Supabase did not create the deletion version.")
	version_id = str(created[0]["id"])
	request_table(
		identity,
		"graph_versions",
		f"?id=eq.{version_id}&is_sealed=eq.false",
		method="PATCH",
		payload={"is_sealed": True},
		prefer="return=minimal",
	)
	set_graph_active_support(identity, graph_id, next_support)
	delete_graph_document_data(identity, document)
	return {"version": {**created[0], "snapshot": snapshot, "is_sealed": True}, "deleted_document_id": document_id}


async def create_graph_version_from_batch(
	identity: SupabaseIdentity,
	graph_id: str,
	document_ids: list[str],
	client,
	max_nodes_per_document: int | None = None,
	min_mentions: int = 1,
) -> dict:
	graph_documents = list_graph_documents(identity, graph_id)
	documents_by_id = {str(document["id"]): document for document in graph_documents}
	selected_ids = list(dict.fromkeys(str(document_id) for document_id in document_ids))
	if not selected_ids:
		raise HTTPException(status_code=422, detail="Choose at least one document for this graph version.")
	if any(document_id not in documents_by_id for document_id in selected_ids):
		raise HTTPException(status_code=404, detail="A selected document does not belong to this graph.")
	if any(documents_by_id[document_id].get("status") != "chunked" for document_id in selected_ids):
		raise HTTPException(status_code=409, detail="Only chunked documents can be added to a graph version.")

	all_versions = request_table(
		identity,
		"graph_versions",
		f"?select=id,version_number,is_sealed&graph_id=eq.{graph_id}&order=version_number.desc",
	)
	version_number = max((int(row["version_number"]) for row in all_versions), default=0) + 1
	week_key = f"Week {version_number}"
	for document_id in selected_ids:
		documents_by_id[document_id]["week"] = week_key
	latest_sealed = next((row for row in all_versions if row.get("is_sealed")), None)
	latest_snapshot = load_graph_version(identity, graph_id, str(latest_sealed["id"])) if latest_sealed else None
	chunks_by_document = {
		document_id: _read_document_chunks(identity, document_id)
		for document_id in documents_by_id
	}
	engine = StudyGraphEngine(collections=None, client=client, graph_id=graph_id)
	concept_support, relationship_support, previous_status = _restore_engine(
		identity,
		graph_id,
		engine,
		graph_documents,
		chunks_by_document,
		latest_snapshot,
	)
	for document_id in selected_ids:
		document = documents_by_id[document_id]
		chunks = [
			{
				"chunk_id": str(row["id"]),
				"text": row["content"],
				"pages": row.get("pages") or [],
				"source": document["filename"],
			}
			for row in chunks_by_document[document_id]
		]
		engine.uploads_by_week[f"{week_key}:{document_id}"] = document["filename"]
		engine.week_counter = version_number + 1
		extraction_batches = make_extraction_batches(chunks)
		batch_tasks = []
		batch_contexts = []
		for batch_index, batch in enumerate(extraction_batches):
			known_values = await asyncio.gather(
				*[
					asyncio.to_thread(engine.relevant_known_concepts, chunk["text"])
					for chunk in batch
				]
			)
			known = {
				chunk["chunk_id"]: value
				for chunk, value in zip(batch, known_values)
			}
			batch_contexts.append((batch, known))
			batch_tasks.append(extract_with_exp_backoff(
				engine,
				batch,
				known_concepts_by_chunk=known,
				batch_index=batch_index + 1,
				total_batches=len(extraction_batches),
			))
		outcomes = await asyncio.gather(*batch_tasks, return_exceptions=True)
		failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
		if failures:
			set_graph_document_status(identity, document_id, "chunked", str(failures[0]))
			raise HTTPException(status_code=502, detail="Document extraction failed; no graph version was created.") from failures[0]
		for (batch, _known), data in zip(batch_contexts, outcomes):
			batch_pages = sorted({page for chunk in batch for page in (chunk.get("pages") or [])})
			before_concepts, before_edges = len(engine.concepts), len(engine.edges)
			await asyncio.to_thread(
				engine.add_to_graph,
				data,
				document_id,
				week_key,
				max_nodes_per_doc=max_nodes_per_document,
				pages=batch_pages,
			)
			for concept in engine.concepts[before_concepts:]:
				concept_support.extend({
			"source_name": concept["name"],
			"document_id": document_id,
			"chunk_id": chunk["chunk_id"],
			"source_week": version_number,
				} for chunk in batch)
			for edge in engine.edges[before_edges:]:
				chunk_id = edge.get("chunk_id")
				if chunk_id:
					relationship_support.append({
						"source_from": edge["from"],
						"source_to": edge["to"],
						"source_relationship_type": edge["relation"],
						"document_id": document_id,
						"chunk_id": chunk_id,
						"summary": edge.get("summary") or "",
						"context": edge.get("context") or "",
						"source_week": version_number,
					})
		engine.known_concepts.update(
			concept["name"] for concept in engine.concepts
			if concept["source"] == document_id and concept.get("week") == week_key
		)
		await asyncio.to_thread(engine.rebuild_known_concept_embeddings)
		if min_mentions > 1:
			await asyncio.to_thread(
				engine.prune_low_value_concepts,
				document_id,
				week_key,
				min_mentions=min_mentions,
			)

	await asyncio.to_thread(engine.deduplicate_extracted_edges)
	await asyncio.to_thread(engine.fuse_graph)
	if engine.graph_status != "active":
		raise HTTPException(status_code=500, detail="Graph fusion did not complete.")

	for support in relationship_support:
		for endpoint in (support["source_from"], support["source_to"]):
			concept_support.append({
				"source_name": endpoint,
				"document_id": support["document_id"],
				"chunk_id": support["chunk_id"],
				"source_week": support.get("source_week") or version_number,
			})
	canonical_names = {
		_canonical_name(engine, concept["name"])
		for concept in engine.concepts
	}
	canonical_names.update(
		_canonical_name(engine, endpoint)
		for edge in engine.fused_edges
		for endpoint in (edge["from"], edge["to"])
	)
	# Descriptions are extracted alongside concepts. Prefer the first grounded
	# description for each canonical concept and only use the old generation
	# path as a fallback for concepts with no extracted description.
	concept_descriptions = {}
	for concept in engine.concepts:
		name = _canonical_name(engine, concept["name"])
		description = (concept.get("description") or "").strip()
		if name and description and name not in concept_descriptions:
			concept_descriptions[name] = description[:1000]

	description_targets = [
		{"name": name}
		for name in sorted(canonical_names)
		if name and name not in concept_descriptions
	]
	if description_targets:
		fallback_descriptions = await _generate_concept_descriptions(
			runtime_state.client,
			description_targets,
			concept_support,
			chunks_by_document,
			engine,
		)
		concept_descriptions.update(fallback_descriptions)
	# Carry forward definitions for concepts restored from the previous version
	# when no new chunk evidence was available for them.
	for previous in (latest_snapshot or {}).get("concepts", []):
		name = graph_fusion.normalize_concept(previous.get("name", ""))
		if name and previous.get("description") and name not in concept_descriptions:
			concept_descriptions[name] = previous["description"]
	new_version = _persist_version(
		identity,
		graph_id,
		version_number,
		engine,
		concept_support,
		relationship_support,
		previous_status,
		documents_by_id,
		concept_descriptions,
	)
	for document_id in selected_ids:
		set_graph_document_status(identity, document_id, "processed", None, week=week_key)
	return {"version": new_version, "concepts": len(engine.concepts), "relationships": len(engine.fused_edges)}
