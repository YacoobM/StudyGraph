from __future__ import annotations

from fastapi import HTTPException

from repositories.supabase_graphs import list_graph_versions, load_graph_version
from repositories.supabase_search import search_graph_chunks
from repositories.users import SupabaseIdentity
from app.config import RAG_MODEL
from utils.rag_utils import (
	build_rag_system_prompt,
	build_rag_user_prompt,
	call_rag_llm,
	filter_sources_by_chunks_used,
	prioritize_graph,
)


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

	chunks = search_graph_chunks(identity, graph_id, query, top_k=5)
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
		header = f"[Chunk {len(context_blocks) + 1}: {citation}]" if citation else f"[Chunk {len(context_blocks) + 1}]"
		context_blocks.append(f"{header}\n{chunk['text']}")
		citations.extend(pages)
		sources.append({
			"fileName": chunk["source"],
			"page_number": pages[0] if pages else 1,
			"snippet": chunk["text"][:120] + ("..." if len(chunk["text"]) > 120 else ""),
		})

	context_text = "\n\n".join(context_blocks)
	graph_section = "Related concepts and connections:\n" + "\n".join(
		f"  • {line.strip()}" for line in graph_lines
	) if graph_lines else ""
	system_prompt = build_rag_system_prompt(prioritize_graph(chunks))
	user_prompt = build_rag_user_prompt(context_text, query, graph_section)
	try:
		result = call_rag_llm(client, RAG_MODEL, system_prompt, user_prompt)
		answer = result["answer"]
		used_sources = filter_sources_by_chunks_used(sources, set(result["chunks_used"]))
	except Exception as error:
		raise HTTPException(status_code=502, detail="The AI Service failed to answer this query.") from error

	unique_pages = sorted(set(citations))
	return {
		"answer": answer,
		"citations": unique_pages,
		"citation_label": _format_pages(unique_pages),
		"sources": used_sources,
		"chunks_used": len(used_sources),
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
