from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException

from app.config import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
from repositories.users import SupabaseIdentity
from services.embeddings import EMBEDDING_DIMENSIONS, encode_texts


RPC_NAME = "search_graph_document_chunks"


def search_graph_chunks(
	identity: SupabaseIdentity,
	graph_id: str,
	query: str,
	top_k: int = 3,
) -> list[dict]:
	if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
		raise HTTPException(status_code=503, detail="Supabase is not configured.")
	if not query.strip():
		return []
	if top_k < 1:
		raise ValueError("top_k must be positive")
	query_vector = encode_texts([query])[0]
	if len(query_vector) != EMBEDDING_DIMENSIONS:
		raise ValueError("Query embedding has the wrong dimension.")

	request = Request(
		f"{SUPABASE_URL}/rest/v1/rpc/{RPC_NAME}",
		data=json.dumps({
			"query_embedding": "[" + ",".join(format(value, ".9g") for value in query_vector) + "]",
			"target_graph_id": graph_id,
			"match_count": top_k,
		}).encode(),
		headers={
			"Authorization": f"Bearer {identity.access_token}",
			"apikey": SUPABASE_PUBLISHABLE_KEY,
			"Content-Type": "application/json",
			"Accept": "application/json",
		},
		method="POST",
	)
	try:
		with urlopen(request, timeout=10) as response:
			payload = json.loads(response.read())
	except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
		raise HTTPException(status_code=502, detail="Unable to search Supabase document chunks.") from error

	if not isinstance(payload, list):
		raise HTTPException(status_code=502, detail="Supabase returned an invalid chunk search response.")
	return [
		{
			"chunk_id": row["chunk_id"],
			"document_id": row["document_id"],
			"source": row["filename"],
			"text": row["content"],
			"pages": row.get("pages") or [],
			"similarity": float(row["similarity"]),
		}
		for row in payload
	]