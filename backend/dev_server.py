"""
StudyGraph dev server.

Why this exists: backend/main.py raises at import time if GROQ_API_KEY is
missing, and needs spacy / chromadb / sentence-transformers / easyocr / torch.
That makes frontend work impossible without the full stack and a paid key.

This server gives the frontend the same HTTP contract as main.py, with:
  - REAL auth + document management (auth_documents.py, SQLite-backed)
  - STUBBED concept extraction / graph / RAG, matching main.py's exact
    response shapes so no frontend code has to change when you point it at
    the real backend

Run:  python dev_server.py          (defaults to port 8000, same as main.py)
Real: uvicorn main:app --port 8000  (needs GROQ_API_KEY + requirements.txt)

Every stubbed route is marked STUB in its docstring and returns
"stub": true in its payload so nothing silently passes for real extraction.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from auth_documents import router as auth_router
from quiz_service import router as quiz_router, set_graph_source

app = FastAPI(
    title="StudyGraph API (dev)",
    description="Real auth + documents; stubbed extraction. Mirrors main.py's contract.",
    version="0.1.0-dev",
)

# Same CORS rule as main.py: any localhost port. file:// pages send
# Origin: null, so allow that too or the HTML cannot call this from disk.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(http://localhost:\d+|http://127\.0\.0\.1:\d+|null)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(quiz_router)

# Quizzes are generated from whatever the engine currently holds.
set_graph_source(lambda: engine)

UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# stub engine state — mirrors StudyGraphEngine's public surface only
# ---------------------------------------------------------------------------

class StubEngine:
    def __init__(self) -> None:
        self.week_counter = 1
        self.uploads_by_week: dict[str, str] = {}
        self.pipeline_status: dict = {"stage": "idle"}
        self.concepts: list[dict] = []
        self.edges: list[dict] = []


engine: StubEngine | None = None

# Deterministic sample graph so the frontend always has something to draw.
SAMPLE_CONCEPTS = [
    "Cellular respiration", "Glycolysis", "Krebs cycle", "Electron transport chain",
    "ATP synthase", "Proton gradient", "Oxidative phosphorylation", "Mitochondrion",
    "NADH", "Pyruvate",
]
SAMPLE_EDGES = [
    ("Cellular respiration", "Glycolysis", "begins with"),
    ("Glycolysis", "Pyruvate", "produces"),
    ("Pyruvate", "Krebs cycle", "feeds"),
    ("Krebs cycle", "NADH", "yields"),
    ("NADH", "Electron transport chain", "donates electrons to"),
    ("Electron transport chain", "Proton gradient", "establishes"),
    ("Proton gradient", "ATP synthase", "drives"),
    ("ATP synthase", "Oxidative phosphorylation", "completes"),
    ("Mitochondrion", "Cellular respiration", "hosts"),
]

WEEK_COLORS = ["#C97C46", "#8E9089", "#22503C", "#C1652F"]


@app.get("/health")
async def health_check():
    """Matches main.py exactly."""
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), max_nodes: int = None, min_mentions: int = 1):
    """
    STUB of main.py's /api/upload.

    Contract preserved: responds immediately with the acknowledgment shape,
    then advances pipeline_status in a background task exactly like
    _run_upload_pipeline does, so the frontend's poll loop is exercised for
    real.
    """
    global engine
    if engine is None:
        engine = StubEngine()

    safe_filename = Path(file.filename or "upload.pdf").name
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Could not read that file.")

    (UPLOADS_DIR / safe_filename).write_bytes(data)

    current_week_key = f"Week {engine.week_counter}"
    engine.week_counter += 1
    engine.uploads_by_week[current_week_key] = safe_filename

    engine.pipeline_status = {
        "stage": "running", "operation": "upload",
        "week": current_week_key, "filename": safe_filename, "error": None,
    }
    asyncio.create_task(_stub_pipeline(engine, safe_filename, current_week_key))

    # main.py reports page count; stub reports a plausible one from file size.
    pages = max(1, len(data) // 2000)
    return {
        "status": "processing",
        "message": f"Received {pages} pages — processing in the background.",
        "week": current_week_key,
        "filename": safe_filename,
        "stub": True,
    }


async def _stub_pipeline(eng: StubEngine, filename: str, week_key: str) -> None:
    """Walks the same stages main.py's background task reports."""
    try:
        for stage_op in ("seeding", "extracting", "building graph"):
            eng.pipeline_status = {
                "stage": "running", "operation": stage_op,
                "week": week_key, "filename": filename, "error": None,
            }
            await asyncio.sleep(0.4)

        for name in SAMPLE_CONCEPTS:
            eng.concepts.append(
                {"name": name, "source": filename, "week": week_key, "pages": [1]}
            )
        for a, b, rel in SAMPLE_EDGES:
            eng.edges.append({"from": a, "to": b, "relation": rel, "summary": "", "context": ""})

        eng.pipeline_status = {
            "stage": "done", "operation": "upload",
            "week": week_key, "filename": filename, "error": None,
            "total_concepts": len({c["name"] for c in eng.concepts}),
            "total_edges": len(eng.edges),
        }
    except Exception as exc:  # keep the poll loop from hanging forever
        eng.pipeline_status = {
            "stage": "error", "operation": "upload",
            "week": week_key, "filename": filename, "error": str(exc),
        }


@app.get("/api/upload/status")
async def get_pipeline_status():
    """Matches main.py exactly."""
    global engine
    if engine is None:
        return {"stage": "idle"}
    return engine.pipeline_status


@app.get("/api/graph")
async def get_graph():
    """
    STUB of main.py's /api/graph.

    Returns the same {nodes, edges} GraphResponse shape. Node dicts carry the
    id/label/color/weeks keys the real endpoint emits for week colour coding.
    """
    global engine
    if engine is None or not engine.concepts:
        return {"nodes": [], "edges": [], "stub": True}

    by_name: dict[str, set] = {}
    for c in engine.concepts:
        by_name.setdefault(c["name"], set()).add(c["week"])

    nodes = []
    for idx, (name, weeks) in enumerate(by_name.items()):
        week_num = min(int(w.split()[-1]) for w in weeks)
        nodes.append({
            "id": name,
            "label": name,
            "color": WEEK_COLORS[(week_num - 1) % len(WEEK_COLORS)],
            "weeks": sorted(weeks),
            "appears_in_weeks": len(weeks),
            "sources": sorted({c["source"] for c in engine.concepts if c["name"] == name}),
            "degree": sum(1 for e in engine.edges if name in (e["from"], e["to"])),
        })

    edges = [
        {"from": e["from"], "to": e["to"], "relation": e["relation"]}
        for e in engine.edges
        if e["from"] in by_name and e["to"] in by_name
    ]
    return {"nodes": nodes, "edges": edges, "stub": True}


class QueryRequest(BaseModel):
    query: str


@app.post("/api/query")
async def query_rag(request: QueryRequest):
    """
    STUB of main.py's /api/query. Returns the RAGResponse shape
    (answer / citations / citation_label / chunks_used / chunks / subgraph).
    Retrieval is a keyword match over the sample graph, not real RAG.
    """
    global engine
    if engine is None or not engine.concepts:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    q = request.query.lower().strip()
    if not q:
        raise HTTPException(status_code=422, detail="Ask a question to search your sources.")

    hits = [n for n in {c["name"] for c in engine.concepts} if any(
        w in n.lower() for w in q.split() if len(w) > 3
    )]
    matched = hits or SAMPLE_CONCEPTS[:2]

    related = [e for e in engine.edges if e["from"] in matched or e["to"] in matched][:5]
    relation_text = "; ".join(f"{e['from']} {e['relation']} {e['to']}" for e in related)

    return {
        "answer": (
            f"[STUB ANSWER — no LLM was called] Your sources connect "
            f"{', '.join(matched[:3])} as follows: {relation_text or 'no edges recorded yet'}."
        ),
        "citations": [1],
        "citation_label": f"{engine.uploads_by_week.get('Week 1', 'source')} p.1",
        "chunks_used": len(matched),
        "chunks": [{"text": f"Sample chunk mentioning {m}", "page": 1} for m in matched[:3]],
        "subgraph": {
            "nodes": [{"id": m, "label": m} for m in matched],
            "edges": [{"from": e["from"], "to": e["to"], "relation": e["relation"]} for e in related],
        },
        "stub": True,
    }


@app.post("/api/fuse")
async def fuse_knowledge_graph():
    """STUB of main.py's /api/fuse."""
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    return {"status": "ok", "fused_edges": len(engine.edges), "stub": True}


@app.get("/api/documents/file/{filename}")
async def get_document_file(filename: str):
    """STUB of main.py's file route — reports presence rather than streaming."""
    path = UPLOADS_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="That file is not on the server.")
    return {"filename": path.name, "size_bytes": path.stat().st_size, "stub": True}


@app.get("/api/_routes")
async def list_routes():
    """
    Dev convenience: what this server actually serves.

    Reads the OpenAPI schema rather than app.routes — in FastAPI 0.141
    app.routes does not include routes mounted via include_router, so
    iterating it silently omits every /api/auth and /api/documents route.
    """
    paths = app.openapi().get("paths", {})
    return {
        "routes": sorted(
            f"{method.upper()} {path}"
            for path, ops in paths.items()
            for method in ops
        )
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"StudyGraph dev server -> http://localhost:{port}")
    print("  REAL:    /api/auth/*, /api/documents*")
    print("  STUBBED: /api/upload, /api/graph, /api/query, /api/fuse")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
