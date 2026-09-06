import sys
import logging
import time
from pathlib import Path
from collections import defaultdict, Counter

backend_dir = Path(__file__).resolve().parents[1]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import json
from app.logging_config import configure_logging
from groq import Groq
from chromadb import Client
from schemas.graph import GraphResponse
from schemas.query import QueryRequest
from api.routes.auth import router as auth_router
from api.routes.documents import router as documents_router
from api.routes.quiz import router as quiz_router, set_graph_source
from api.routes.upload import router as upload_router
from app import state as runtime_state
from services.graph_runtime import save_graph_json

from services import evolution_data, graph_fusion

load_dotenv()
configure_logging()

chroma = Client()

if not os.getenv("GROQ_API_KEY"):
    raise RuntimeError("GROQ_API_KEY is not configured!")

app = FastAPI(
    title="StudyGraph API",
    description="FastAPI backend for StudyGraph LLM integration",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(http://localhost:\d+|http://127\.0\.0\.1:\d+|null)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth + document management (see backend/auth_documents.py)
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(upload_router)

app.include_router(quiz_router)

client = Groq(api_key=os.getenv("GROQ_API_KEY"), max_retries=0)
runtime_state.configure_runtime(client, chroma)
set_graph_source(lambda: runtime_state.engine)

LLM_MODEL = "qwen/qwen3.6-27b"
RAG_MODEL = "openai/gpt-oss-20b"
logger = logging.getLogger(__name__)


@app.get("/health")
async def health_check():
    return {"status": "ok"}

def _source_page_fields(pages):
    """
    pages: sorted list of distinct page ints a concept appeared on within one
    source file. Returns the jump target for the PDF viewer (first page) plus
    a compact human-readable label ("43", "43-48", "43, 47, 50").
    """
    if not pages:
        return {"page": None, "pageLabel": None}
    if len(pages) == 1:
        label = str(pages[0])
    elif pages == list(range(pages[0], pages[-1] + 1)):
        label = f"{pages[0]}-{pages[-1]}"
    else:
        label = ", ".join(str(p) for p in pages)
    return {"page": pages[0], "pageLabel": label}


@app.get("/api/graph")
async def get_graph():
    """Get current knowledge graph (uses fused graph if available), with
    week-based color coding for the evolution feature."""
    engine = runtime_state.engine
    if engine is None:
        return GraphResponse(nodes=[], edges=[])

    week_color_map = evolution_data.get_week_color_map(engine)

    working_edges = engine.fused_edges if engine.fused_edges else engine.edges

    concept_relations = {}
    for e in working_edges:
        from_node = e["from"]
        to_node = e["to"]
        if from_node not in concept_relations:
            concept_relations[from_node] = []
        if to_node not in concept_relations:
            concept_relations[to_node] = []
        concept_relations[from_node].append(f"{e['relation']} â†’ {to_node}")
        concept_relations[to_node].append(f"{from_node} â†’ {e['relation']}")

    aggregated_concepts = {}
    for c in engine.concepts:
        canonical_name = engine.canonical_nodes.get(c["name"], c["name"])
        if canonical_name not in aggregated_concepts:
            aggregated_concepts[canonical_name] = {"sources": defaultdict(set), "weeks": set()}
        aggregated_concepts[canonical_name]["sources"][c["source"]].update(
            p for p in (c.get("pages") or []) if isinstance(p, int)
        )
        week = c.get("week")
        if week:
            aggregated_concepts[canonical_name]["weeks"].add(week)

    nodes = []
    for name, data in aggregated_concepts.items():
        appears_in_weeks = len(data["weeks"])
        node_color = evolution_data.DEFAULT_COLOR

        if appears_in_weeks > 1:
            node_color = evolution_data.MULTI_WEEK_COLOR
        elif appears_in_weeks == 1:
            first_week = list(data["weeks"])[0]
            node_color = week_color_map.get(first_week, evolution_data.DEFAULT_COLOR)

        if not any(n["name"] == name for n in nodes):
            nodes.append({
                "id": name.replace(" ", "_"),
                "name": name,
                "source": [
                    {"file": f, **_source_page_fields(sorted(page_set))}
                    for f, page_set in data["sources"].items()
                ],
                "description": "; ".join(concept_relations.get(name, []))[:150] or name,
                "is_canonical": name in engine.canonical_nodes.values(),
                "color": node_color,
                "appears_in_weeks": appears_in_weeks,
                # The weeks themselves, not just how many. The client needs
                # these to drive the week slider and to colour concepts that
                # arrived in the week being viewed.
                "weeks": sorted(
                    data["weeks"],
                    key=lambda w: int(w.split(" ")[1]) if " " in w else 0,
                ),
            })

    valid_node_ids = {n["id"] for n in nodes}

    edges = [
        {
            "id": f"{e['from']}_to_{e['to']}",
            "source": e["from"].replace(" ", "_"),
            "target": e["to"].replace(" ", "_"),
            "label": e["relation"],
            "relation": e["relation"],
            "summary": e.get("summary", ""),
            "context": e.get("context", ""),
            "chunk_ids": e.get("chunk_ids", [e["chunk_id"]] if e.get("chunk_id") else []),
            "conflict_resolved": e.get("conflict_resolved", False)
        }
        for e in working_edges
        if e["from"].replace(" ", "_") in valid_node_ids and e["to"].replace(" ", "_") in valid_node_ids
    ]

    if engine.inferred_edges:
        for e in engine.inferred_edges:
            if (e["from"].replace(" ", "_") in valid_node_ids and
                    e["to"].replace(" ", "_") in valid_node_ids):
                edges.append({
                    "id": f"{e['from']}_to_{e['to']}_inferred",
                    "source": e["from"].replace(" ", "_"),
                    "target": e["to"].replace(" ", "_"),
                    "label": e["relation"],
                    "relation": e["relation"],
                    "summary": e.get("summary", ""),
                    "context": e.get("context", ""),
                    "conflict_resolved": False,
                    "is_inferred": True
                })

    graph_save_path = save_graph_json(nodes, edges, {
        "canonical_nodes": len(engine.canonical_nodes),
        "fused_edges": len(engine.fused_edges),
        "inferred_edges": len(engine.inferred_edges)
    })

    legend = {
        "weeks": [{"week": week, "color": color} for week, color in week_color_map.items()],
        "multi_week": {"label": "Spans multiple weeks", "color": evolution_data.MULTI_WEEK_COLOR},
        "no_week_data": {"label": "No week data", "color": evolution_data.DEFAULT_COLOR},
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "is_fused": bool(engine.fused_edges),
        "graph_saved_to": graph_save_path,
        "legend": legend,
        "stats": {
            "canonical_nodes": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges)
        }
    }


@app.post("/api/query")
async def query_rag(request: QueryRequest):
    """Query the knowledge graph with RAG"""
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later."
        )
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    try:
        result = engine.rag_query(request.query)
        return result
    except HTTPException as e:
        if e.status_code in [401, 402, 403, 429]:
            raise HTTPException(
                status_code=503,
                detail="The service is temporarily down. Please try again soon."
            )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/api/fuse")
async def fuse_knowledge_graph():
    """
    Trigger the Global Knowledge Fusion pipeline (Phase 3 of BATCH-STREAM-BATCH).
    Call this endpoint after all documents are uploaded.
    """
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    fusion_started = time.perf_counter()
    try:
        logger.info("fusion_request_started concepts=%d edges=%d", len(engine.concepts), len(engine.edges))
        engine.fuse_graph()

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        fused_nodes = [{"name": c["name"], "source": c["source"]} for c in engine.concepts]
        graph_save_path = save_graph_json(fused_nodes, working_edges, {
            "canonical_nodes": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges)
        })

        logger.info(
            "fusion_request_complete canonical_nodes=%d fused_edges=%d duration_ms=%.1f",
            len(engine.canonical_nodes),
            len(engine.fused_edges),
            (time.perf_counter() - fusion_started) * 1000,
        )
        return {
            "status": "success",
            "message": "Knowledge graph fusion complete",
            "pipeline_phase": "batch_fusion",
            "canonical_nodes_created": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges),
            "total_concepts": len(engine.concepts),
            "total_edges": len(engine.edges),
            "graph_saved_to": graph_save_path
        }
    except Exception as e:
        logger.exception(
            "fusion_request_failed duration_ms=%.1f",
            (time.perf_counter() - fusion_started) * 1000,
        )
        raise HTTPException(status_code=400, detail=str(e))


# --- Concept-evolution endpoints ---

@app.get("/api/evolution/graph")
async def get_evolution_graph_data(week: str = None):
    """Get the main evolution graph data, optionally filtered by week."""
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    return evolution_data.get_graph_data(engine, week)


@app.get("/api/evolution/concept/{concept_name}/description")
async def get_evolution_concept_description(concept_name: str):
    """Cheap tier: a single grounded summary from the concept's
    earliest-appearance week only. Meant to back a plain node click."""
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    description = await evolution_data.get_concept_description(engine, concept_name, client)
    if not description:
        raise HTTPException(status_code=404, detail="Concept not found")
    return description


@app.get("/api/evolution/concept/{concept_name}")
async def get_evolution_concept_detail(concept_name: str):
    """Full tier: every week's summary plus every week-to-week comparison.
    Generates and caches lazily on first request â€” meant to back the
    explicit "show concept evolution" action."""
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    details = await evolution_data.get_concept_detail(engine, concept_name, client)
    if not details:
        raise HTTPException(status_code=404, detail="Concept not found")
    return details


@app.get("/api/evolution/subgraph/{concept_name}")
async def get_evolution_concept_subgraph(concept_name: str):
    """Get the D3-ready subgraph for a concept's weekly evolution."""
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    subgraph = evolution_data.get_concept_subgraph(engine, concept_name)
    if not subgraph:
        raise HTTPException(status_code=404, detail="Concept not found")
    return subgraph


@app.post("/api/evolution/concept/{concept_name}/regenerate")
async def regenerate_evolution_concept(concept_name: str):
    """
    Clears cached summaries/comparisons for a concept so the next request
    for its description or full detail regenerates them from Groq instead
    of reusing whatever's cached.
    """
    engine = runtime_state.engine
    if engine is None:
        raise HTTPException(status_code=404, detail="No graph loaded")

    occurrences = [c for c in engine.concepts if engine.canonical_nodes.get(c["name"], c["name"]) == concept_name]
    if not occurrences:
        raise HTTPException(status_code=404, detail="Concept not found")

    raw_week_pairs = {(occ["name"], occ["week"]) for occ in occurrences}
    for key in raw_week_pairs:
        engine.concept_week_summaries.pop(key, None)
        engine.degraded_summary_keys.discard(key)
    engine.concept_overall_comparison.pop(concept_name, None)
    engine.concept_consecutive_comparisons.pop(concept_name, None)

    return {"ok": True, "cleared": len(raw_week_pairs)}
 
 
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)

