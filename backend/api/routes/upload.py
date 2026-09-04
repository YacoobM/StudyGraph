from __future__ import annotations

import asyncio
import os
import time
import traceback
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import UPLOADS_DIR
from app.state import get_engine
from services import evolution_data
from services.document_service import extract_text
from services.graph_runtime import (
    save_extraction_result,
    save_graph_json,
    save_seed_entities,
)
from groq import RateLimitError


router = APIRouter(tags=["upload"])


async def extract_with_exp_backoff(engine, text, known_concepts=None, retries=5):
    delay = 5
    for _ in range(retries):
        try:
            return await engine.extract_knowledge(text, known_concepts=known_concepts)
        except RateLimitError:
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")


async def _run_upload_pipeline(engine, client, pages, filename, week_key, upload_start, max_nodes, min_mentions):
    try:
        safe_name = Path(filename).name
        engine.generate_seeds(pages)
        save_seed_entities(engine.seed_entities)
        chunks = engine.get_chunks(pages, source=safe_name, week_key=week_key)
        engine.rebuild_known_concept_embeddings()

        extraction_tasks = [
            extract_with_exp_backoff(
                engine,
                chunk["text"],
                known_concepts=engine.relevant_known_concepts(chunk["text"]),
            )
            for chunk in chunks
        ]
        results = await asyncio.gather(*extraction_tasks)

        for index, data in enumerate(results):
            engine.add_to_graph(
                data,
                filename,
                week_key,
                max_nodes_per_doc=max_nodes,
                pages=chunks[index].get("pages"),
            )

        if min_mentions > 1:
            engine.prune_low_value_concepts(filename, week_key, min_mentions=min_mentions)

        engine.known_concepts.update(
            concept["name"]
            for concept in engine.concepts
            if concept["source"] == filename and concept["week"] == week_key
        )
        doc_week_pairs = {
            (concept["name"], concept["week"])
            for concept in engine.concepts
            if concept["source"] == filename and concept["week"] == week_key
        }
        await evolution_data.precompute_week_summaries(engine, client, doc_week_pairs)

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        graph_save_path = save_graph_json(
            [{"name": concept["name"], "source": concept["source"]} for concept in engine.concepts],
            working_edges,
        )
        upload_latency = round(time.time() - upload_start, 3)
        save_extraction_result(
            {
                "number of chunks": len(chunks),
                "latency": upload_latency,
                "time per chunk": upload_latency / len(chunks) if chunks else 0,
            },
            engine.concepts,
            working_edges,
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
    except Exception as error:
        print(f"Background upload pipeline failed for {filename}: {error}")
        traceback.print_exc()
        engine.pipeline_status = {
            "stage": "error",
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            "error": str(error),
        }


@router.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), max_nodes: int = None, min_mentions: int = 1):
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(status_code=503, detail="The AI Service is temporarily down. Please try again later.")

    from app.state import client, set_engine

    upload_start = time.time()
    engine = get_engine()
    if client is None:
        raise HTTPException(status_code=503, detail="The AI Service is temporarily down. Please try again later.")

    safe_filename = Path(file.filename or "upload.pdf").name
    try:
        file_bytes = await file.read()
        pages = extract_text(file_bytes, safe_filename)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))

    (UPLOADS_DIR / safe_filename).write_bytes(file_bytes)
    current_week_key = f"Week {engine.week_counter}"
    engine.week_counter += 1
    engine.uploads_by_week[current_week_key] = safe_filename
    engine.pipeline_status = {
        "stage": "running",
        "operation": "upload",
        "week": current_week_key,
        "filename": safe_filename,
        "error": None,
    }
    set_engine(engine)
    asyncio.create_task(
        _run_upload_pipeline(
            engine,
            client,
            pages,
            safe_filename,
            current_week_key,
            upload_start,
            max_nodes,
            min_mentions,
        )
    )
    return {
        "status": "processing",
        "message": f"Received {len(pages)} pages — processing in the background.",
        "week": current_week_key,
        "filename": safe_filename,
    }


@router.get("/api/upload/status")
async def get_pipeline_status():
    from app.state import engine
    return engine.pipeline_status if engine is not None else {"stage": "idle"}


@router.get("/api/files/{filename}")
async def serve_uploaded_file(filename: str):
    safe_name = Path(filename).name
    upload_path = UPLOADS_DIR / safe_name
    if not upload_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(upload_path, media_type="application/pdf", headers={"Content-Disposition": f"inline; filename=\"{safe_name}\""})


@router.get("/api/documents/file/{filename}")
async def get_document_file(filename: str):
    safe_name = Path(filename).name
    pdf_path = UPLOADS_DIR / safe_name
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=safe_name)


__all__ = ["router"]