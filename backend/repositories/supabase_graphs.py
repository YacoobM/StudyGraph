import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote

from fastapi import HTTPException

from app.config import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
from repositories.users import SupabaseIdentity
from services.graph_fusion import normalize_concept
from services.evolution_data import _call_with_retry, summarize_comparisons_batch, synthesize_overall_evolution

REST_PAGE_SIZE = 1000
logger = logging.getLogger(__name__)


def request_table(
	identity: SupabaseIdentity,
	table: str,
	query: str,
	method: str = "GET",
	payload: dict | list[dict] | None = None,
	prefer: str | None = None,
) -> list[dict]:
	if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
		raise HTTPException(status_code=503, detail="Supabase is not configured.")

	headers = {
		"Authorization": f"Bearer {identity.access_token}",
		"apikey": SUPABASE_PUBLISHABLE_KEY,
		"Accept": "application/json",
	}
	if payload is not None:
		headers["Content-Type"] = "application/json"
	if prefer:
		headers["Prefer"] = prefer

	body = json.dumps(payload).encode() if payload is not None else None
	rows = []
	offset = 0
	while True:
		page_query = query
		if method == "GET":
			page_query += f"&limit={REST_PAGE_SIZE}&offset={offset}"
		request = Request(
			f"{SUPABASE_URL}/rest/v1/{table}{page_query}",
			data=body,
			headers=headers,
			method=method,
		)
		try:
			with urlopen(request, timeout=10) as response:
				body = response.read()
				response_data = json.loads(body) if body else []
		except HTTPError as error:
			error_body = error.read().decode("utf-8", errors="replace")
			logger.error(
				"Supabase request failed method=%s table=%s query=%s status=%s body=%s",
				method,
				table,
				query,
				error.code,
				error_body[:1000],
			)
			raise HTTPException(
				status_code=502,
				detail="Unable to load graph data from Supabase.",
			) from error
		except (URLError, TimeoutError, json.JSONDecodeError) as error:
			logger.error(
				"Supabase request failed method=%s table=%s query=%s error=%s",
				method,
				table,
				query,
				error,
			)
			raise HTTPException(
				status_code=502,
				detail="Unable to load graph data from Supabase.",
			) from error

		if not isinstance(response_data, list):
			raise HTTPException(status_code=502, detail="Supabase returned an invalid graph response.")
		if method != "GET":
			return response_data

		rows.extend(response_data)
		if len(response_data) < REST_PAGE_SIZE:
			return rows
		offset += REST_PAGE_SIZE


def list_graphs(identity: SupabaseIdentity) -> list[dict]:
	return request_table(
		identity,
		"graphs",
		"?select=id,topic_id,name,created_at,updated_at&order=created_at.desc,id.desc",
	)


def create_graph(identity: SupabaseIdentity, name: str, topic_id: str | None = None) -> dict:
	payload = {"user_id": identity.auth_user_id, "name": name}
	if topic_id:
		owned_topic = request_table(identity, "topics", f"?select=id&id=eq.{topic_id}")
		if not owned_topic:
			raise HTTPException(status_code=404, detail="Topic not found.")
		payload["topic_id"] = topic_id
	rows = request_table(
		identity,
		"graphs",
		"?select=id,topic_id,name,created_at,updated_at",
		method="POST",
		payload=payload,
		prefer="return=representation",
	)
	if len(rows) != 1:
		raise HTTPException(status_code=502, detail="Supabase did not return the created graph.")
	return rows[0]


def update_graph_name(identity: SupabaseIdentity, graph_id: str, name: str) -> dict:
	rows = request_table(
		identity,
		"graphs",
		f"?id=eq.{graph_id}&select=id,topic_id,name,created_at,updated_at",
		method="PATCH",
		payload={"name": name},
		prefer="return=representation",
	)
	if len(rows) != 1:
		raise HTTPException(status_code=404, detail="Graph not found.")
	return rows[0]


def get_graph_active_support(identity: SupabaseIdentity, graph_id: str) -> dict:
	rows = request_table(
		identity,
		"graphs",
		f"?select=id,active_support&id=eq.{graph_id}",
	)
	if not rows:
		raise HTTPException(status_code=404, detail="Graph not found.")
	return rows[0].get("active_support") or {"concepts": {}, "relationships": {}}


def set_graph_active_support(identity: SupabaseIdentity, graph_id: str, active_support: dict) -> None:
	request_table(
		identity,
		"graphs",
		f"?id=eq.{graph_id}",
		method="PATCH",
		payload={"active_support": active_support},
		prefer="return=minimal",
	)


def list_graph_versions(identity: SupabaseIdentity, graph_id: str) -> list[dict]:
	return request_table(
		identity,
		"graph_versions",
		f"?select=id,version_number,created_at,is_sealed&graph_id=eq.{graph_id}&is_sealed=eq.true&order=version_number.desc,id.desc",
	)


def load_graph_version(
	identity: SupabaseIdentity,
	graph_id: str,
	version_id: str,
) -> dict:
	versions = request_table(
		identity,
		"graph_versions",
		f"?select=id,graph_id,version_number,created_at,is_sealed,snapshot&graph_id=eq.{graph_id}&id=eq.{version_id}&is_sealed=eq.true",
	)
	if not versions:
		raise HTTPException(status_code=404, detail="Graph version not found.")

	version = versions[0]
	snapshot = version.get("snapshot") or {"concepts": [], "relationships": []}
	concept_provenance = request_table(
		identity,
		"concept_provenance",
		"?select=id,graph_version_id,concept_id,document_id,document_filename,chunk_id,source_name,source_excerpt,page_start,page_end,source_week,created_at"
		f"&graph_version_id=eq.{version_id}&order=created_at.asc,id.asc",
	)
	if not concept_provenance:
		concept_provenance = snapshot.get("concept_provenance") or []
	relationship_provenance = request_table(
		identity,
		"relationship_provenance",
		"?select=id,graph_version_id,relationship_id,document_id,document_filename,chunk_id,source_from,source_to,source_relationship_type,summary,context,source_week,created_at"
		f"&graph_version_id=eq.{version_id}&order=created_at.asc,id.asc",
	)
	if not relationship_provenance:
		relationship_provenance = snapshot.get("relationship_provenance") or []
	return {
		"version": version,
		"concepts": snapshot.get("concepts") or [],
		"relationships": snapshot.get("relationships") or [],
		"concept_provenance": concept_provenance,
		"relationship_provenance": relationship_provenance,
	}


async def get_graph_concept_evolution(identity: SupabaseIdentity, graph_id: str, concept_name: str, client=None) -> dict:
	"""Build the existing evolution response shape from versioned provenance."""
	target = normalize_concept(concept_name)
	versions = list_graph_versions(identity, graph_id)
	if not versions:
		return {"comparison": f"{concept_name} appears across 0 graph version(s).", "weeks_data": [], "consecutive_comparisons": []}
	version = versions[0]
	version_id = str(version["id"])
	loaded = load_graph_version(identity, graph_id, version_id)
	concept = next((row for row in loaded["concepts"] if normalize_concept(row.get("normalized_name") or row.get("name", "")) == target), None)
	if not concept:
		return {"comparison": f"{concept_name} appears across 0 graph version(s).", "weeks_data": [], "consecutive_comparisons": []}
	rows = [
		row for row in loaded["concept_provenance"]
		if str(row.get("concept_id")) == str(concept["id"])
	]
	rows.sort(key=lambda row: (int(row.get("source_week") or version["version_number"]), row.get("document_filename") or "", str(row.get("id") or "")))
	chunk_by_id = {}
	chunk_by_document = {}
	for row in rows:
		chunk_id = row.get("chunk_id")
		if chunk_id:
			chunks = request_table(identity, "document_chunks", f"?select=id,content&id=eq.{chunk_id}")
			if chunks:
				chunk_by_id[str(chunk_id)] = chunks[0].get("content") or ""
		elif row.get("document_id"):
			document_id = str(row["document_id"])
			if document_id not in chunk_by_document:
				chunk_by_document[document_id] = request_table(
					identity, "document_chunks",
					f"?select=id,content&document_id=eq.{document_id}&order=chunk_index.asc",
				)
	grouped = {}
	for row in rows:
		grouped.setdefault(int(row.get("source_week") or version["version_number"]), []).append(row)
	weeks = [{"version": week, "rows": values} for week, values in sorted(grouped.items())]

	weeks_data = []
	for item in weeks:
		docs = ", ".join(sorted({row["document_filename"] for row in item["rows"]}))
		evidence_parts = []
		for row in item["rows"]:
			if row.get("chunk_id"):
				evidence_parts.append(chunk_by_id.get(str(row["chunk_id"]), ""))
			else:
				evidence_parts.extend(chunk.get("content") or "" for chunk in chunk_by_document.get(str(row.get("document_id")), []))
		evidence = "\n".join(evidence_parts)[:1200]
		summary = f"Supported by {docs}."
		if client and evidence:
			summary_prompt = (
				"Summarize how this concept is presented in the supplied study evidence. "
				"Write 1-3 sentences, grounded only in the evidence; mention its meaning and relevant connections."
			)
			generated = await _call_with_retry(client, summary_prompt, f"CONCEPT: {concept_name}\nSOURCES: {docs}\nEVIDENCE:\n{evidence}", max_tokens=220)
			if generated and generated.strip():
				summary = generated.strip()
		weeks_data.append({"week": f"Week {item['version']}", "summary": summary, "is_fallback": summary == f"Supported by {docs}."})
	comparisons = []
	for previous, current in zip(weeks_data, weeks_data[1:]):
		comparisons.append({
			"from_week": previous["week"],
			"to_week": current["week"],
			"comparison": f"The supporting sources changed from {previous['summary']} to {current['summary']}",
		})
	if client and comparisons:
		requests = []
		for previous, current in zip(weeks, weeks[1:]):
			from_text = "\n".join(str(row.get("source_excerpt") or "") for row in previous["rows"])
			to_text = "\n".join(str(row.get("source_excerpt") or "") for row in current["rows"])
			requests.append({"key": f"{previous['version']}|{current['version']}", "concept_name": concept_name,
				"from_week": f"Week {previous['version']}", "from_text": from_text, "to_week": f"Week {current['version']}", "to_text": to_text})
		generated = await summarize_comparisons_batch(client, requests)
		for comparison, request in zip(comparisons, requests):
			if generated.get(request["key"]):
				comparison["comparison"] = generated[request["key"]]
	comparison = comparisons[0]["comparison"] if len(weeks_data) == 2 and comparisons else None
	if client and len(weeks_data) > 2 and comparisons:
		comparison = await synthesize_overall_evolution(client, concept_name, comparisons)
	return {
		"comparison": comparison or f"{concept_name} appears across {len(weeks_data)} graph version(s).",
		"weeks_data": weeks_data,
		"consecutive_comparisons": comparisons,
	}
