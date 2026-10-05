from pathlib import Path
import asyncio
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from uuid import UUID

from repositories.supabase_documents import (
    create_graph_document,
    list_graph_documents,
    persist_document_chunks,
    set_graph_document_status,
    signed_graph_document_url,
)
from repositories.users import SupabaseIdentity, current_supabase_identity
from services.chunking import build_document_chunks
from services.document_service import extract_text
from services.embeddings import encode_texts
from services.supabase_graph_processing import delete_graph_document_and_create_version

router = APIRouter(tags=["documents"])

MAX_GRAPH_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_GRAPH_SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md"}


@router.get("/api/graphs/{graph_id}/documents")
def get_graph_documents(
    graph_id: UUID,
    identity: SupabaseIdentity = Depends(current_supabase_identity),
):
    return {"documents": list_graph_documents(identity, str(graph_id))}


@router.get("/api/graphs/{graph_id}/documents/{document_id}/file")
def get_graph_document_file_url(
    graph_id: UUID,
    document_id: UUID,
    identity: SupabaseIdentity = Depends(current_supabase_identity),
):
    return {"url": signed_graph_document_url(identity, str(graph_id), str(document_id))}


@router.delete("/api/graphs/{graph_id}/documents/{document_id}")
async def delete_graph_document(
    graph_id: UUID,
    document_id: UUID,
    identity: SupabaseIdentity = Depends(current_supabase_identity),
):
    return await delete_graph_document_and_create_version(
        identity, str(graph_id), str(document_id)
    )


@router.post("/api/graphs/{graph_id}/documents", status_code=201)
async def post_graph_document(
    graph_id: UUID,
    file: UploadFile = File(...),
    identity: SupabaseIdentity = Depends(current_supabase_identity),
):
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=422, detail="Choose a file to upload.")
    if Path(filename).suffix.lower() not in ALLOWED_GRAPH_SUFFIXES:
        raise HTTPException(status_code=415, detail="That file type is not supported.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="That file is empty.")
    if len(content) > MAX_GRAPH_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is larger than 50 MB.")

    document = create_graph_document(
        identity,
        str(graph_id),
        filename,
        content,
        file.content_type or "application/octet-stream",
    )
    try:
        pages = await extract_text(content, filename)
        chunks = build_document_chunks(pages, filename, document_id=document["id"])
        embeddings = await asyncio.to_thread(
            encode_texts, [chunk["text"] for chunk in chunks]
        )
        persist_document_chunks(identity, document["id"], chunks, embeddings)
        set_graph_document_status(identity, document["id"], "chunked")
        document["status"] = "chunked"
    except Exception as error:
        try:
            set_graph_document_status(identity, document["id"], "error", str(error))
        except HTTPException:
            pass
        raise
    return {"document": document}
