from __future__ import annotations

import asyncio
import os
import time
import mimetypes
from repositories.database import connect
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


from app import state as runtime_state
from services.document_service import extract_text
from app.config import LLM_API_KEY_ENV, UPLOADS_DIR
from app.state import client, get_engine, set_engine
from repositories.documents import _update_document_status

from services.graph_runtime import (
    save_extraction_result,
    save_graph_json,
)
from services.graph_engine import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE

from utils.validate_file import guard_file
from services.llm_gateway import estimate_tokens
from services.llm_provider import RATE_LIMIT_ERRORS

router = APIRouter(tags=["upload"])

from app.logging_config import get_logger
logger = get_logger(__name__)

MAX_INPUT_TOKENS_PER_CALL = 4200
MAX_CHUNKS_PER_BATCH = 4

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/markdown",
}

def make_extraction_batches(chunks):
    batches, current, current_tokens = [], [], 0
    for chunk in chunks:
        chunk_tokens = estimate_tokens(chunk["text"])
        if current and (len(current) >= MAX_CHUNKS_PER_BATCH or current_tokens + chunk_tokens > MAX_INPUT_TOKENS_PER_CALL):
            batches.append(current)
            current, current_tokens = [], 0
        current.append(chunk)
        current_tokens += chunk_tokens
    if current:
        batches.append(current)
    return batches


async def extract_with_exp_backoff(engine, chunks, known_concepts_by_chunk=None, batch_index=None, total_batches=None, retries=5):
    delay = 5
    started = time.perf_counter()
    for attempt in range(1, retries + 1):
        try:
            data = await engine.extract_knowledge_batch(chunks, known_concepts_by_chunk=known_concepts_by_chunk)
            logger.info("extraction_batch_complete batch=%s/%s chunks=%d chars=%d concepts=%d relationships=%d attempts=%d duration_ms=%.1f", batch_index, total_batches, len(chunks), sum(len(chunk["text"]) for chunk in chunks), len(data.get("concepts", [])), len(data.get("relationships", [])), attempt, (time.perf_counter() - started) * 1000)
            return data
        except RATE_LIMIT_ERRORS:
            logger.warning("extraction_batch_rate_limited batch=%s/%s attempt=%d/%d", batch_index, total_batches, attempt, retries)
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")


async def _fuse_when_idle(engine):
    """Fuse the graph once this upload was the last one running."""
    async with runtime_state.fusion_lock:
        if runtime_state.uploads_in_flight():
            return
        done_status = engine.pipeline_status
        if done_status.get("stage") not in ("done", "partially_processed"):
            return

        engine.pipeline_status = {**done_status, "stage": "running", "operation": "fusing"}
        try:
            await asyncio.to_thread(engine.fuse_graph)
        except Exception as error:
            logger.error("Fusion after upload failed: %s", error, exc_info=True)
            done_status = {**done_status, "fusion_error": str(error)}

        if not runtime_state.uploads_in_flight():
            engine.pipeline_status = done_status


async def _pipeline_then_fuse(engine, *args, **kwargs):
    """Run the upload pipeline, then fuse if nothing else is still processing."""
    async with runtime_state.fusion_lock:
        pass
    try:
        await _run_upload_pipeline(engine, *args, **kwargs)
    finally:
        runtime_state.upload_finished()
    await _fuse_when_idle(engine)


def _schedule_upload_pipeline(
    engine,
    client,
    pages,
    filename,
    week_key,
    upload_start,
    max_nodes,
    min_mentions,
    document_id=None,
):
    """Schedule every upload through the shared completion-and-fusion flow."""
    runtime_state.upload_started()
    asyncio.create_task(
        _pipeline_then_fuse(
            engine,
            client,
            pages,
            filename,
            week_key,
            upload_start,
            max_nodes,
            min_mentions,
            document_id=document_id,
        )
    )


async def _run_upload_pipeline(
    engine,
    client,
    pages,
    filename,
    week_key,
    upload_start,
    max_nodes,
    min_mentions,
    document_id=None,
):
    pipeline_started = time.perf_counter()
    phase_started = pipeline_started
    logger.info(
        "upload_pipeline_started filename=%s week=%s pages=%d",
        filename,
        week_key,
        len(pages),
    )

    def set_status(stage, **details):
        engine.pipeline_status.update({
            "stage": stage,
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            **details,
        })
        logger.info(
            "upload_status filename=%s week=%s stage=%s details=%s",
            filename,
            week_key,
            stage,
            details,
        )

    try:
        safe_name = Path(filename).name
        _update_document_status(document_id, "running", week=week_key, error=None)

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
        extraction_batches = make_extraction_batches(chunks)
        batch_contexts = []
        extraction_tasks = []
        for index, batch in enumerate(extraction_batches):
            known_concepts_by_chunk = {
                chunk["chunk_id"]: engine.relevant_known_concepts(chunk["text"])
                for chunk in batch
            }
            batch_contexts.append((batch, known_concepts_by_chunk))
            extraction_tasks.append(
                extract_with_exp_backoff(
                    engine,
                    batch,
                    known_concepts_by_chunk=known_concepts_by_chunk,
                    batch_index=index + 1,
                    total_batches=len(extraction_batches),
                )
            )
        batch_results = await asyncio.gather(*extraction_tasks, return_exceptions=True)
        results = [
            {
                "chunk_ids": [chunk["chunk_id"] for chunk in batch],
                "data": data,
            }
            for batch, data in zip(extraction_batches, batch_results)
            if not isinstance(data, Exception)
        ]
        failed_batches = [
            {
                "batch_id": index + 1,
                "chunk_ids": [chunk["chunk_id"] for chunk in batch],
                "error": str(data),
            }
            for index, ((batch, _known), data) in enumerate(zip(batch_contexts, batch_results))
            if isinstance(data, Exception)
        ]
        for failure in failed_batches:
            error = batch_results[failure["batch_id"] - 1]
            logger.error(
                "extraction_batch_failed batch=%s/%s chunks=%d error_type=%s status_code=%s error=%s headers=%s",
                failure["batch_id"], len(extraction_batches), len(failure["chunk_ids"]),
                type(error).__name__, getattr(error, "status_code", None), str(error),
                getattr(getattr(error, "response", None), "headers", None),
                exc_info=error,
            )
        engine.failed_upload_batches = {
            item["batch_id"]: {
                "batch": batch_contexts[item["batch_id"] - 1][0],
                "known_concepts_by_chunk": batch_contexts[item["batch_id"] - 1][1],
                "filename": filename,
                "week": week_key,
                "document_id": document_id,
                "max_nodes": max_nodes,
            }
            for item in failed_batches
        }
        extraction_duration = time.perf_counter() - phase_started
        set_status(
            "extracting" if failed_batches else "extracted",
            chunks=len(chunks),
            chunks_completed=sum(len(result["chunk_ids"]) for result in results),
            batches=len(extraction_batches),
            batches_completed=len(results),
            failed_batches=failed_batches,
        )
        logger.info(
            "document_extraction_complete filename=%s week=%s chunks=%d duration_ms=%.1f average_chunk_ms=%.1f",
            safe_name,
            week_key,
            len(extraction_batches),
            extraction_duration * 1000,
            extraction_duration * 1000 / len(chunks) if chunks else 0,
        )

        phase_started = time.perf_counter()
        for batch_result in results:
            pages_by_chunk = {
                chunk["chunk_id"]: chunk.get("pages")
                for chunk in chunks
                if chunk["chunk_id"] in batch_result["chunk_ids"]
            }
            data = batch_result["data"]
            batch_pages = sorted({
                page
                for pages in pages_by_chunk.values()
                for page in (pages or [])
            })
            engine.add_to_graph(
                data,
                filename,
                week_key,
                max_nodes_per_doc=max_nodes,
                pages=batch_pages,
            )

        logger.info(
            "document_graph_ingest_complete filename=%s week=%s concepts=%d edges=%d duration_ms=%.1f",
            safe_name,
            week_key,
            len(engine.concepts),
            len(engine.edges),
            (time.perf_counter() - phase_started) * 1000,
        )

        set_status("deduplicating", chunks=len(chunks), chunks_completed=len(chunks))
        engine.deduplicate_extracted_edges()

        if min_mentions > 1:
            engine.prune_low_value_concepts(filename, week_key, min_mentions=min_mentions)

        engine.known_concepts.update(
            concept["name"]
            for concept in engine.concepts
            if concept["source"] == filename and concept["week"] == week_key
        )

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        document_concepts = [
            concept for concept in engine.concepts
            if concept["source"] == filename and concept.get("week") == week_key
        ]
        document_edges = [
            edge for edge in engine.edges
            if edge.get("source") == filename
        ]
        graph_save_path = save_graph_json(
            [{"name": concept["name"], "source": concept["source"]} for concept in engine.concepts],
            working_edges,
            graph_name=engine.graph_name or filename,
        )
        upload_latency = round(time.time() - upload_start, 3)
        save_extraction_result(
            {
                "number of chunks": len(chunks),
                "chunk_size_chars": DEFAULT_CHUNK_SIZE,
                "chunk_overlap_chars": DEFAULT_CHUNK_OVERLAP,
                "actual_chunk_sizes_chars": [len(chunk["text"]) for chunk in chunks],
                "latency": upload_latency,
                "time per chunk": upload_latency / len(chunks) if chunks else 0,
            },
            engine.concepts,
            working_edges,
        )
        engine.upload_latency = upload_latency
        final_status = "partially_processed" if failed_batches else "done"
        engine.pipeline_status = {
            "stage": final_status,
            "status": final_status,
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            "chunks": len(chunks),
            "raw_concepts": len(document_concepts),
            "total_edges": len(document_edges),
            "graph_saved_to": graph_save_path,
            "error": None,
            "latency_seconds": upload_latency,
            "phase_timings": {
                "chunk_extraction_seconds": round(extraction_duration, 3),
                "average_chunk_seconds": round(extraction_duration / len(chunks), 3) if chunks else 0,
                "evolution_deferred": True,
            },
            "failed_batches": failed_batches,
            "retryable": bool(failed_batches),
        }
        _update_document_status(
            document_id,
            final_status,
            week=week_key,
            concepts=len(document_concepts),
            relationships=len(document_edges),
            error=None,
        )
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
        _update_document_status(document_id, "error", error=str(error))


async def start_upload_pipeline(
    file_bytes,
    filename,
    max_nodes=None,
    min_mentions=1,
    document_id=None,
    stored_name=None,
):
    """Start the shared background extraction pipeline for a stored upload."""
    if runtime_state.client is None:
        raise HTTPException(status_code=503, detail="The AI Service is temporarily down. Please try again later.")

    engine = get_engine()
    safe_filename = Path(filename).name
    engine.pipeline_status = {
        "stage": "parsing",
        "operation": "upload",
        "filename": safe_filename,
        "document_id": document_id,
        "error": None,
    }
    try:
        pages = await extract_text(file_bytes, safe_filename)
    except Exception as error:
        _update_document_status(document_id, "error", error=str(error))
        logger.exception("upload_document_read_failed filename=%s document_id=%s", safe_filename, document_id)
        raise

    if stored_name is None:
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

    _schedule_upload_pipeline(
        engine,
        runtime_state.client,
        pages,
        safe_filename,
        current_week_key,
        time.time(),
        max_nodes,
        min_mentions,
        document_id=document_id,
    )
    logger.info(
        "upload_pipeline_scheduled filename=%s week=%s document_id=%s pages=%d",
        safe_filename,
        current_week_key,
        document_id,
        len(pages),
    )
    return current_week_key, safe_filename, pages


@router.post("/api/upload/retry-failed")
async def retry_failed_upload_batches():
    engine = runtime_state.engine
    if engine is None or not engine.failed_upload_batches:
        raise HTTPException(status_code=400, detail="There are no failed upload batches to retry.")
    if runtime_state.client is None:
        raise HTTPException(status_code=503, detail="The AI Service is temporarily down. Please try again later.")

    pending = dict(engine.failed_upload_batches)
    tasks = [
        extract_with_exp_backoff(
            engine,
            item["batch"],
            known_concepts_by_chunk=item["known_concepts_by_chunk"],
            batch_index=batch_id,
            total_batches=len(pending),
        )
        for batch_id, item in pending.items()
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    retry_failures = []
    for (batch_id, item), outcome in zip(pending.items(), outcomes):
        if isinstance(outcome, Exception):
            retry_failures.append({
                "batch_id": batch_id,
                "chunk_ids": [chunk["chunk_id"] for chunk in item["batch"]],
                "error": str(outcome),
            })
            continue

        pages = sorted({
            page
            for chunk in item["batch"]
            for page in (chunk.get("pages") or [])
        })
        engine.add_to_graph(
            outcome,
            item["filename"],
            item["week"],
            max_nodes_per_doc=item["max_nodes"],
            pages=pages,
        )
        engine.failed_upload_batches.pop(batch_id, None)

    if retry_failures:
        engine.failed_upload_batches = {
            failure["batch_id"]: pending[failure["batch_id"]]
            for failure in retry_failures
        }

    batches_by_doc: dict[str, list[dict]] = {}
    for item in pending.values():
        document_id = item["document_id"]
        batches_by_doc.setdefault(document_id, []).append(item)

    final_status = "partially_processed" if retry_failures else "done"

    for doc_id, items in batches_by_doc.items():
        first_item = items[0]
        filename = first_item["filename"]
        week = first_item["week"]

        document_concepts = [c for c in engine.concepts if c["source"] == filename and c.get("week") == week]
        document_edges = [e for e in engine.edges if e.get("source") == filename]

        _update_document_status(
            doc_id,
            final_status,
            week=week,
            concepts=len(document_concepts),
            relationships=len(document_edges),
            error="Some extraction batches failed." if retry_failures else None,
        )

    last_item = next(iter(pending.values()))
    last_filename = last_item["filename"]
    last_week = last_item["week"]
    doc_concepts = [c for c in engine.concepts if c["source"] == last_filename and c.get("week") == last_week]
    doc_edges = [e for e in engine.edges if e.get("source") == last_filename]

    engine.pipeline_status.update({
        "stage": final_status,
        "status": final_status,
        "failed_batches": retry_failures,
        "retryable": bool(retry_failures),
        "raw_concepts": len(doc_concepts),
        "total_edges": len(doc_edges),
    })

    return engine.pipeline_status


@router.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), max_nodes: int = None, min_mentions: int = 1):
    if not os.getenv(LLM_API_KEY_ENV):
        raise HTTPException(status_code=503, detail="The AI Service is temporarily down. Please try again later.")

    upload_start = time.time()
    engine = get_engine()
    if client is None:
        raise HTTPException(status_code=503, detail="The AI Service is temporarily down. Please try again later.")

    safe_filename = Path(file.filename or "upload.pdf").name
    logger.info("upload_request_received filename=%s", safe_filename)
    parse_started = time.perf_counter()
    try:
        file_bytes = await file.read()
        guard_file(file_bytes, safe_filename)
        pages = extract_text(file_bytes, safe_filename)
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("upload_document_read_failed filename=%s", safe_filename)
        raise HTTPException(status_code=400, detail=str(error))

    logger.info(
        "upload_document_read_complete filename=%s bytes=%d pages=%d duration_ms=%.1f",
        safe_filename,
        len(file_bytes),
        len(pages),
        (time.perf_counter() - parse_started) * 1000,
    )

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
    _schedule_upload_pipeline(
        engine,
        client,
        pages,
        safe_filename,
        current_week_key,
        upload_start,
        max_nodes,
        min_mentions,
    )
    logger.info(
        "upload_pipeline_scheduled filename=%s week=%s request_duration_ms=%.1f",
        safe_filename,
        current_week_key,
        (time.time() - upload_start) * 1000,
    )
    return {
        "status": "processing",
        "message": f"Received {len(pages)} pages — processing in the background.",
        "week": current_week_key,
        "filename": safe_filename,
    }


@router.get("/api/upload/status")
async def get_pipeline_status():
    engine = runtime_state.engine
    return engine.pipeline_status if engine is not None else {"stage": "idle"}


@router.get("/api/files/{filename}")
async def serve_uploaded_file(filename: str):
    requested_name = Path(filename).name

    with connect() as conn:
        row = conn.execute(
            """
            SELECT stored_name
            FROM documents
            WHERE filename = ?
            ORDER BY uploaded_at DESC
            LIMIT 1
            """,
            (requested_name,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="File not found")

    stored_name = row["stored_name"]
    upload_path = UPLOADS_DIR / stored_name

    if not upload_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = mimetypes.guess_type(requested_name)[0] or "application/octet-stream"

    return FileResponse(
        upload_path,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{requested_name}"'
        },
    )

@router.get("/api/documents/file/{filename}")
async def get_document_file(filename: str):
    safe_name = Path(filename).name
    pdf_path = UPLOADS_DIR / safe_name
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=safe_name)


__all__ = ["router"]
