from __future__ import annotations

from fastapi import HTTPException

from repositories.supabase_graphs import list_graph_versions, load_graph_version
from repositories.supabase_search import search_graph_chunks
from repositories.users import SupabaseIdentity
from app.config import RAG_MODEL
from services.llm_gateway import estimate_tokens, rag_limiter
from utils.rag_utils import (
	build_rag_system_prompt,
	build_rag_user_prompt,
	call_rag_llm,
	filter_sources_by_chunks_used,
	prioritize_graph,
)

from app.logging_config import get_logger

logger = get_logger(__name__)

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
	"""
	GraphRAG Query: Combine retrieved text chunks with the graph structure
	to improve the quality of responses.
 
	For broad queries with weak text matches, we lean on the graph to provide
	the user with an output.
 
	Returns: dict with answer, citations, sources, subgraph, concepts_mentioned
	and metadata
	"""
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
			"subgraph": {"nodes": [], "edges": [], "is_fused": True},
			"graph_search_depth": 0,
			"concepts_mentioned": [],
			"fusion_status": "fused",
			"retrieval_method": "text",
			"graph_used": False,
		}
 
	# Load the graph snapshot to build context from relationships
	try:
		snapshot = _latest_snapshot(identity, graph_id)
	except HTTPException:
		raise
	except Exception as e:
		logger.exception("Failed to load graph snapshot: %s", e)
		raise HTTPException(status_code=409, detail="Could not load graph data.") from e
 
	# Build concept lookup and graph section for context
	concepts = {str(concept["id"]): concept["name"] for concept in snapshot.get("concepts", [])}
	relationships = snapshot.get("relationships", [])
	
	graph_section = ""
	subgraph_nodes = []
	subgraph_edges = []
	
	if relationships:
		graph_lines = ["Related concepts and connections:"]
		for e in relationships[:8]:
			source_name = concepts.get(str(e["source_concept_id"]), "?")
			target_name = concepts.get(str(e["target_concept_id"]), "?")
			relation = e.get("relationship_type", "relates to")
			graph_lines.append(f"  • {source_name} --[{relation}]--> {target_name}")
		graph_section = "\n".join(graph_lines)
		subgraph_edges = relationships
		# Collect unique concept nodes referenced in edges
		concept_ids = set()
		for e in relationships:
			concept_ids.add(str(e["source_concept_id"]))
			concept_ids.add(str(e["target_concept_id"]))
		subgraph_nodes = [
			{"id": cid, "name": concepts.get(cid, "?")}
			for cid in concept_ids
		]
 
	# Build context blocks from chunks
	context_blocks = []
	sources = []
	citations = []
	
	for chunk in chunks:
		pages = chunk.get("pages", [])
		citation = _format_pages(pages)
		header = f"[Chunk {len(context_blocks) + 1}: {citation}]" if citation else f"[Chunk {len(context_blocks) + 1}]"
		context_blocks.append(f"{header}\n{chunk['text']}")
		citations.extend(pages)
		sources.append({
			"document_id": chunk.get("document_id"),
			"fileName": chunk.get("source", "document.pdf"),
			"page_number": pages[0] if pages else 1,
			"snippet": (
				(chunk["text"][:120] + "...") if len(chunk["text"]) > 120 else chunk["text"]
			),
		})
 
	context_text = "\n\n".join(context_blocks)
	unique_pages = sorted(set(citations))
	use_graph_primarily = prioritize_graph(chunks)
 
	system_prompt = build_rag_system_prompt(prioritize_graph=use_graph_primarily)
	user_prompt = build_rag_user_prompt(context_text, query, graph_section)
	# Call the LLM and parse response
	try:
		rag_limiter.acquire_sync(
			estimate_tokens(system_prompt) + estimate_tokens(user_prompt),
			1500,
		)
		result = call_rag_llm(client, RAG_MODEL, system_prompt, user_prompt)
		answer = result["answer"]
		used_sources = filter_sources_by_chunks_used(sources, set(result["chunks_used"]))
		concepts_mentioned = result.get("concepts_mentioned", [])
	except (AttributeError, KeyError, ValueError) as e:
		logger.exception("rag_query_response_parse_failed error=%s", e)
		answer = "Error: Could not parse response from language model. Please try again"
		used_sources = []
		concepts_mentioned = []
	except Exception as e:
		logger.exception("rag_query_llm_failed error_type=%s", type(e).__name__)
		answer = f"Error querying language model: {type(e).__name__}"
		used_sources = []
		concepts_mentioned = []
 
	return {
		"answer": answer,
		"citations": unique_pages,
		"citation_label": _format_pages(unique_pages),
		"sources": used_sources,
		"chunks_used": len(used_sources),
		"chunks": [
			{
				"text": chunk["text"],
				"pages": chunk.get("pages", []),
				"citation": _format_pages(chunk.get("pages", [])),
				"similarity": chunk.get("similarity", 0),
			}
			for chunk in chunks
		],
		"subgraph": {
			"nodes": subgraph_nodes,
			"edges": subgraph_edges,
			"is_fused": True,
		},
		"graph_search_depth": 1 if relationships else 0,
		"concepts_mentioned": concepts_mentioned,
		"fusion_status": "fused",
		"retrieval_method": "text+graph" if graph_section else "text",
		"graph_used": bool(graph_section),
	}
