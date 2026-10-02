from __future__ import annotations

from fastapi import HTTPException

from repositories.supabase_graphs import list_graph_versions, load_graph_version
from repositories.supabase_search import search_graph_chunks
from repositories.users import SupabaseIdentity


def _format_pages(pages: list[int]) -> str:
	if not pages:
		return ""
	if len(pages) == 1:
		return f"p. {pages[0]}"
	if pages == list(range(pages[0], pages[-1] + 1)):
		return f"pp. {pages[0]}-{pages[-1]}"
	return "pp. " + ", ".join(str(page) for page in pages)


def _latest_snapshot(identity: SupabaseIdentity, graph_id: str) -> dict:
	versions = list_graph_versions(identity, graph_id)
	if not versions:
		raise HTTPException(status_code=409, detail="This graph has no sealed version yet.")
	return load_graph_version(identity, graph_id, str(versions[0]["id"]))


def answer_graph_query(identity: SupabaseIdentity, graph_id: str, query: str, client) -> dict:
	if client is None:
		raise HTTPException(status_code=503, detail="The AI Service is temporarily down.")

	chunks = search_graph_chunks(identity, graph_id, query, top_k=3)
	if not chunks:
		return {
			"answer": "No relevant text chunks found in this graph. Try a different query.",
			"citations": [],
			"citation_label": "",
			"sources": [],
			"chunks_used": 0,
			"chunks": [],
			"subgraph": {"nodes": [], "edges": [], "is_fused": False},
			"graph_search_depth": 0,
		}

	snapshot = _latest_snapshot(identity, graph_id)
	concepts = {str(concept["id"]): concept["name"] for concept in snapshot["concepts"]}
	graph_lines = [
		f"  {concepts.get(edge['source_concept_id'], '?')} [{edge['relationship_type']}] "
		f"{concepts.get(edge['target_concept_id'], '?')}"
		for edge in snapshot["relationships"][:10]
	]
	context_blocks = []
	sources = []
	citations = []
	for chunk in chunks:
		pages = chunk["pages"]
		citation = _format_pages(pages)
		context_blocks.append(f"[{citation or 'unknown page'}]\n{chunk['text'][:400]}")
		citations.extend(pages)
		sources.append({
			"fileName": chunk["source"],
			"page_number": pages[0] if pages else 1,
			"snippet": chunk["text"][:120],
		})

	system_prompt = """You are a concise study assistant. Answer questions directly and briefly.
Always cite page numbers for facts from the text using [p. N] or [pp. N-M].
Answer in 2-3 sentences maximum and do not use information outside the supplied graph."""
	user_prompt = (
		"TEXT FROM THIS GRAPH:\n"
		+ "\n\n".join(context_blocks)
		+ "\n\nGRAPH CONNECTIONS:\n"
		+ ("\n".join(graph_lines) if graph_lines else "(none)")
		+ f"\n\nQUESTION: {query}\n\nAnswer directly and cite page numbers."
	)
	try:
		response = client.chat.completions.create(
			messages=[
				{"role": "system", "content": system_prompt},
				{"role": "user", "content": user_prompt},
			],
			model="openai/gpt-oss-20b",
		)
		answer = response.choices[0].message.content
	except Exception as error:
		raise HTTPException(status_code=502, detail="The AI Service failed to answer this query.") from error

	unique_pages = sorted(set(citations))
	return {
		"answer": answer,
		"citations": unique_pages,
		"citation_label": _format_pages(unique_pages),
		"sources": sources,
		"chunks_used": len(chunks),
		"chunks": [
			{
				"text": chunk["text"][:200] + ("..." if len(chunk["text"]) > 200 else ""),
				"pages": chunk["pages"],
				"citation": _format_pages(chunk["pages"]),
				"similarity": chunk["similarity"],
			}
			for chunk in chunks
		],
		"subgraph": {"nodes": [], "edges": snapshot["relationships"], "is_fused": True},
		"graph_search_depth": 1 if snapshot["relationships"] else 0,
	}