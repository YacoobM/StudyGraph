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
		version_id = str(latest["version"]["id"])
		request_table(
			identity,
			"graph_versions",
			f"?id=eq.{version_id}",
			method="PATCH",
			payload={"snapshot": {"concepts": latest["concepts"], "relationships": latest["relationships"]}},
			prefer="return=minimal",
		)
		for concept in degraded:
			if concept.get("name") in updated_names:
				request_table(
					identity,
					"concepts",
					f"?graph_version_id=eq.{version_id}&id=eq.{concept['id']}",
					method="PATCH",
					payload={"status": "active"},
					prefer="return=minimal",
				)
	return {"updated": len(updated_names), "concepts": updated_names}
