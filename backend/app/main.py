import sys
from pathlib import Path
from collections import defaultdict, Counter

backend_dir = Path(__file__).resolve().parents[1]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import json
from app.config import OUTPUT_DIR, UPLOADS_DIR
from services.document_service import extract_text
from groq import Groq, RateLimitError
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from datetime import datetime
import time
import asyncio
from chromadb import Client
from schemas.extraction import ExtractionResult
from schemas.graph import GraphResponse
from schemas.query import QueryRequest
from api.routes.auth import router as auth_router
from api.routes.documents import router as documents_router
from api.routes.quiz import router as quiz_router, set_graph_source
from services.graph_engine import StudyGraphEngine
from services.graph_runtime import (
    call_llm,
    get_collection,
    save_extraction_result,
    save_graph_json,
    save_seed_entities,
)

from services import evolution_data, graph_fusion

load_dotenv()

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

app.include_router(quiz_router)
set_graph_source(lambda: engine)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

engine = None

LLM_MODEL = "qwen/qwen3.6-27b"
RAG_MODEL = "openai/gpt-oss-20b"

async def extract_with_exp_backoff(text, known_concepts=None, retries=5):
    """
    Helper method to extract concepts and relationships with exponential
    backoff.
    """
    delay = 5

    for attempt in range(retries):
        try:
            return await (engine.extract_knowledge(text, known_concepts=known_concepts))
        except RateLimitError as e:
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")

# API Endpoints
@app.get("/health")
async def health_check():
    return {"status": "ok"}


async def _run_upload_pipeline(engine, pages, filename, week_key, upload_start, max_nodes, min_mentions):
    """
    Everything slow and LLM-dependent about an upload: seed generation (first
    upload only), chunk extraction, graph building, and â€” for every concept
    that survives pruning â€” its cheap-tier description (see
    evolution_data.precompute_week_summaries).

    The full evolution tier (every week's summary + every week-to-week
    comparison â€” see get_concept_detail) is deliberately NOT precomputed
    here â€” it's gated behind the explicit "View Full Evolution" action.
    """
    try:
        safe_name = Path(filename).name

        engine.generate_seeds(pages)
        save_seed_entities(engine.seed_entities)

        chunks = engine.get_chunks(pages, source=safe_name, week_key=week_key)

        # Snapshot known_concepts before this document contributes anything,
        # so extraction is only nudged toward concepts from earlier weeks.
        engine.rebuild_known_concept_embeddings()

        extraction_tasks = [
            extract_with_exp_backoff(
                chunk["text"],
                known_concepts=engine.relevant_known_concepts(chunk["text"]),
            )
            for chunk in chunks
        ]
        results = await asyncio.gather(*extraction_tasks)

        for i, data in enumerate(results):
            engine.add_to_graph(data, filename, week_key, max_nodes_per_doc=max_nodes, pages=chunks[i].get("pages"))
            print(f"Processed chunk {i + 1}/{len(results)}")

        if min_mentions > 1:
            engine.prune_low_value_concepts(filename, week_key, min_mentions=min_mentions)

        engine.known_concepts.update(
            c["name"] for c in engine.concepts
            if c["source"] == filename and c["week"] == week_key
        )

        doc_week_pairs = {
            (c["name"], c["week"]) for c in engine.concepts
            if c["source"] == filename and c["week"] == week_key
        }
        await evolution_data.precompute_week_summaries(engine, client, doc_week_pairs)

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        graph_save_path = save_graph_json(
            [{"name": c["name"], "source": c["source"]} for c in engine.concepts],
            working_edges
        )

        upload_latency = round(time.time() - upload_start, 3)
        save_extraction_result(
            {
                "number of chunks": len(chunks),
                "latency": upload_latency,
                "time per chunk": upload_latency / len(chunks) if chunks else 0
            },
            engine.concepts,
            working_edges
        )
        engine.upload_latency = upload_latency

        engine.pipeline_status = {
            "stage": "done",
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            "chunks": len(chunks),
            "seeds_generated": len(engine.seed_entities),
            "seed_entities": engine.seed_entities,
            "raw_concepts": len(engine.concepts),
            "total_edges": len(working_edges),
            "graph_saved_to": graph_save_path,
            "error": None,
        }
    except Exception as e:
        print(f"Background upload pipeline failed for {filename}: {e}")
        import traceback
        traceback.print_exc()
        engine.pipeline_status = {
            "stage": "error", "operation": "upload",
            "week": week_key, "filename": filename, "error": str(e),
        }


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), max_nodes: int = None, min_mentions: int = 1):
    """
    Upload a PDF. Only the fast, local, deterministic steps (read the file,
    save it to disk, parse its text, assign a week) happen before this
    responds. Extraction and graph building happen afterward in a
    background task (see _run_upload_pipeline). Poll GET /api/upload/status
    for progress.

    max_nodes: optional hard cap on distinct concepts admitted from this
    document (default: no cap).

    min_mentions: "key concept" threshold for pruning low-value concepts
    after extraction. Default 1 == pruning disabled. Raise to 2+ to drop
    concepts extracted only once and not in seed_entities.
    """
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later."
        )

    global engine
    upload_start = time.time()

    if engine is None:
        engine = StudyGraphEngine(get_collection(chroma, 1), client, LLM_MODEL, RAG_MODEL)

    safe_filename = Path(file.filename).name

    try:
        file_bytes = await file.read()
        pages = extract_text(file_bytes, safe_filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    (UPLOADS_DIR / safe_filename).write_bytes(file_bytes)

    current_week_key = f"Week {engine.week_counter}"
    engine.week_counter += 1
    engine.uploads_by_week[current_week_key] = safe_filename

    engine.pipeline_status = {
        "stage": "running", "operation": "upload",
        "week": current_week_key, "filename": safe_filename, "error": None,
    }
    asyncio.create_task(
        _run_upload_pipeline(engine, pages, safe_filename, current_week_key, upload_start, max_nodes, min_mentions)
    )

    return {
        "status": "processing",
        "message": f"Received {len(pages)} pages â€” processing in the background.",
        "week": current_week_key,
        "filename": safe_filename,
    }


@app.get("/api/upload/status")
async def get_pipeline_status():
    """Poll target for the background upload pipeline (see StudyGraphEngine.pipeline_status)."""
    global engine
    if engine is None:
        return {"stage": "idle"}
    return engine.pipeline_status

@app.get("/api/files/{filename}")
async def serve_uploaded_file(filename: str):
    safe_name = Path(filename).name
    upload_path = UPLOADS_DIR / safe_name
    if not upload_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        upload_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{safe_name}\""},
    )

@app.get("/api/documents/file/{filename}")
async def get_document_file(filename: str):
    """Serves a previously-uploaded PDF back (e.g. for the 'open source at page N' viewer)."""
    safe_name = Path(filename).name
    pdf_path = UPLOADS_DIR / safe_name
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=safe_name)


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
    global engine
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
    global engine
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
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    try:
        engine.fuse_graph()

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        fused_nodes = [{"name": c["name"], "source": c["source"]} for c in engine.concepts]
        graph_save_path = save_graph_json(fused_nodes, working_edges, {
            "canonical_nodes": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges)
        })

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
        raise HTTPException(status_code=400, detail=str(e))


# --- Concept-evolution endpoints ---

@app.get("/api/evolution/graph")
async def get_evolution_graph_data(week: str = None):
    """Get the main evolution graph data, optionally filtered by week."""
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    return evolution_data.get_graph_data(engine, week)


@app.get("/api/evolution/concept/{concept_name}/description")
async def get_evolution_concept_description(concept_name: str):
    """Cheap tier: a single grounded summary from the concept's
    earliest-appearance week only. Meant to back a plain node click."""
    global engine
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
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    details = await evolution_data.get_concept_detail(engine, concept_name, client)
    if not details:
        raise HTTPException(status_code=404, detail="Concept not found")
    return details


@app.get("/api/evolution/subgraph/{concept_name}")
async def get_evolution_concept_subgraph(concept_name: str):
    """Get the D3-ready subgraph for a concept's weekly evolution."""
    global engine
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
    global engine
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

