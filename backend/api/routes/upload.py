from __future__ import annotations

import asyncio
import logging
import os
import time
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
logger = logging.getLogger(__name__)


async def extract_with_exp_backoff(engine, text, known_concepts=None, chunk_index=None, total_chunks=None, retries=5):
    delay = 5
    started = time.perf_counter()
    for attempt in range(1, retries + 1):
        try:
            data = await engine.extract_knowledge(text, known_concepts=known_concepts)
            logger.info(
                "chunk_extraction_complete chunk=%s/%s chars=%d concepts=%d relationships=%d attempts=%d duration_ms=%.1f",
                chunk_index,
                total_chunks,
                len(text),
                len(data.get("concepts", [])),
                len(data.get("relationships", [])),
                attempt,
                (time.perf_counter() - started) * 1000,
            )
            return data
        except RateLimitError:
            logger.warning(
                "chunk_extraction_rate_limited chunk=%s/%s attempt=%d/%d",
                chunk_index,
                total_chunks,
                attempt,
                retries,
            )
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")


async def _run_upload_pipeline(engine, client, pages, filename, week_key, upload_start, max_nodes, min_mentions):
    pipeline_started = time.perf_counter()
    phase_started = pipeline_started

    def set_status(stage, **details):
        engine.pipeline_status.update({
            "stage": stage,
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            **details,
        })

    try:
        safe_name = Path(filename).name
        set_status("seeding", error=None)
        engine.generate_seeds(pages)
        save_seed_entities(engine.seed_entities)
        seed_duration = time.perf_counter() - phase_started
        logger.info(
            "document_seeding_complete filename=%s week=%s pages=%d seeds=%d duration_ms=%.1f",
            safe_name,
            week_key,
            len(pages),
            len(engine.seed_entities),
            seed_duration * 1000,
        )

        phase_started = time.perf_counter()
        set_status("chunking")
        chunks = engine.get_chunks(pages, source=safe_name, week_key=week_key)
        engine.rebuild_known_concept_embeddings()
        logger.info(
            "document_chunking_complete filename=%s week=%s chunks=%d duration_ms=%.1f",
            safe_name,
            week_key,
            len(chunks),
            (time.perf_counter() - phase_started) * 1000,
        )

        phase_started = time.perf_counter()
        set_status("extracting", chunks=len(chunks), chunks_completed=0)
        extraction_tasks = [
            extract_with_exp_backoff(
                engine,
                chunk["text"],
                known_concepts=engine.relevant_known_concepts(chunk["text"]),
                chunk_index=index + 1,
                total_chunks=len(chunks),
            )
            for index, chunk in enumerate(chunks)
        ]
        results = await asyncio.gather(*extraction_tasks)
        extraction_duration = time.perf_counter() - phase_started
        set_status("extracted", chunks=len(chunks), chunks_completed=len(chunks))
        logger.info(
            "document_extraction_complete filename=%s week=%s chunks=%d duration_ms=%.1f average_chunk_ms=%.1f",
            safe_name,
            week_key,
            len(chunks),
            extraction_duration * 1000,
            extraction_duration * 1000 / len(chunks) if chunks else 0,
        )

        phase_started = time.perf_counter()
        for index, data in enumerate(results):
            engine.add_to_graph(
                data,
                filename,
                week_key,
                max_nodes_per_doc=max_nodes,
                pages=chunks[index].get("pages"),
            )

        logger.info(
            "document_graph_ingest_complete filename=%s week=%s concepts=%d edges=%d duration_ms=%.1f",
            safe_name,
            week_key,
            len(engine.concepts),
            len(engine.edges),
            (time.perf_counter() - phase_started) * 1000,
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
        phase_started = time.perf_counter()
        set_status("evolution_summaries", concepts=len(doc_week_pairs))
        await evolution_data.precompute_week_summaries(engine, client, doc_week_pairs)
        logger.info(
            "document_evolution_complete filename=%s week=%s concepts=%d duration_ms=%.1f",
            safe_name,
            week_key,
            len(doc_week_pairs),
            (time.perf_counter() - phase_started) * 1000,
        )

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
            "latency_seconds": upload_latency,
            "phase_timings": {
                "seed_generation_seconds": round(seed_duration, 3),
                "chunk_extraction_seconds": round(extraction_duration, 3),
                "average_chunk_seconds": round(extraction_duration / len(chunks), 3) if chunks else 0,
            },
        }
    except Exception as error:
        logger.exception(
            "document_pipeline_failed filename=%s week=%s duration_ms=%.1f",
            filename,
            week_key,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        set_status(
            "error",
            error=str(error),
            latency_seconds=round(time.perf_counter() - pipeline_started, 3),
        )


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