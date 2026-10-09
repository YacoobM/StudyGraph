from fastapi import HTTPException

from repositories.supabase_graphs import list_graph_versions, load_graph_version, request_table
from repositories.users import SupabaseIdentity
from services.evolution_data import _call_with_retry


async def refresh_degraded_concept_definitions(identity: SupabaseIdentity, graph_id: str, client) -> dict:
	if client is None:
		raise HTTPException(status_code=503, detail="The language model is not configured.")
	versions = list_graph_versions(identity, graph_id)
	if not versions:
		return {"updated": 0, "concepts": []}
	latest = load_graph_version(identity, graph_id, str(versions[0]["id"]))
	degraded = [concept for concept in latest["concepts"] if concept.get("status") == "needs_regeneration"]
	if not degraded:
		return {"updated": 0, "concepts": []}

	provenance = latest.get("concept_provenance") or []
	updated_names = []
	for concept in degraded:
		rows = [row for row in provenance if str(row.get("concept_id")) == str(concept["id"])]
		evidence = []
		for row in rows:
			row_evidence = []
			if row.get("chunk_id"):
				chunks = request_table(identity, "document_chunks", f"?select=content&id=eq.{row['chunk_id']}")
				if chunks:
					row_evidence.append(chunks[0].get("content") or "")
			if not row_evidence and row.get("source_excerpt"):
				row_evidence.append(row["source_excerpt"])
			evidence.extend(row_evidence)
		if not evidence:
			continue
		evidence_text = "\n\n".join(evidence)[:8000]
		prompt = (
			"Write a concise, accurate definition for this study concept using only the supplied evidence. "
			"Explain what it means and its important connections in 2-4 sentences. Do not mention the deletion, "
			"the refresh process, or missing sources."
		)
		definition = await _call_with_retry(
			client,
			prompt,
			f"CONCEPT: {concept.get('name', '')}\nEVIDENCE:\n{evidence_text}",
			max_tokens=260,
		)
		if definition and definition.strip():
			concept["description"] = definition.strip()
			concept["status"] = "active"
			updated_names.append(concept.get("name", ""))

	if updated_names:
		new_version_number = int(latest["version"].get("version_number") or 0) + 1
		rows = request_table(
			identity,
			"graph_versions",
			"?select=id,graph_id,version_number,created_at,is_sealed,snapshot",
			method="POST",
			payload={
				"graph_id": graph_id,
				"version_number": new_version_number,
				"is_sealed": False,
				"snapshot": {
					"concepts": latest["concepts"],
					"relationships": latest["relationships"],
					"concept_provenance": latest.get("concept_provenance") or [],
					"relationship_provenance": latest.get("relationship_provenance") or [],
				},
			},
			prefer="return=representation",
		)
		if len(rows) != 1:
			raise HTTPException(status_code=502, detail="Supabase did not create the refreshed graph version.")
		version_id = str(rows[0]["id"])
		request_table(
			identity,
			"graph_versions",
			f"?id=eq.{version_id}&is_sealed=eq.false",
			method="PATCH",
			payload={"is_sealed": True},
			prefer="return=minimal",
		)
		# Read back the saved snapshot. Do not report success based only on the
		# in-memory object. The old sealed version remains immutable by design;
		# the refreshed version must be the one the UI serves.
		saved = load_graph_version(identity, graph_id, version_id)
		remaining = [
			concept.get("name", "")
			for concept in saved["concepts"]
			if concept.get("status") == "needs_regeneration"
		]
		if remaining:
			raise HTTPException(
				status_code=502,
				detail=(
					"Supabase did not persist all refreshed concept definitions. "
					f"Still degraded: {', '.join(remaining)}"
				),
			)
	return {"updated": len(updated_names), "concepts": updated_names, "remaining": [], "version_id": version_id if updated_names else None}
