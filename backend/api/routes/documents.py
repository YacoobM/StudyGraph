from __future__ import annotations

import sqlite3
import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.config import UPLOADS_DIR
from repositories.database import connect
from repositories.documents import document_public
from repositories.users import current_user
from schemas.documents import DocumentPatch
from api.routes.upload import start_upload_pipeline
from services.quiz_service import document_title


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md"}

router = APIRouter(tags=["documents"])
logger = logging.getLogger(__name__)


@router.get("/api/documents")
async def list_documents(user: sqlite3.Row = Depends(current_user)):
	with connect() as conn:
		rows = conn.execute(
			"SELECT * FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC",
			(user["id"],),
		).fetchall()

	# `title` is the same name the Quiz tab shows: the document's own title
	# line from its stored text, else the cleaned filename.
	docs = [{**document_public(row), "title": document_title(row["filename"])} for row in rows]
	return {
		"documents": docs,
		"totals": {
			"count": len(docs),
			"concepts": sum(document["concepts"] for document in docs),
			"relationships": sum(document["relationships"] for document in docs),
			"size_kb": sum(document["size_kb"] for document in docs),
		},
	}


@router.post("/api/documents", status_code=201)
async def create_document(
	file: UploadFile = File(...),
	week: str = Form(default=""),
	max_nodes: int | None = Form(default=None),
	min_mentions: int = Form(default=1),
	user: sqlite3.Row = Depends(current_user),
):
	filename = Path(file.filename or "").name
	logger.info("document_create_received filename=%s user_id=%s", filename, user["id"])
	if not filename:
		raise HTTPException(status_code=422, detail="Choose a file to upload.")

	suffix = Path(filename).suffix.lower()
	if suffix not in ALLOWED_SUFFIXES:
		allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
		raise HTTPException(status_code=415, detail=f"That file type is not supported. Use: {allowed}")

	data = await file.read()
	if len(data) == 0:
		raise HTTPException(status_code=422, detail="That file is empty.")
	if len(data) > MAX_UPLOAD_BYTES:
		raise HTTPException(
			status_code=413,
			detail=f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
		)

	stored_name = f"{user['id']}_{int(time.time() * 1000)}_{filename}"
	(UPLOADS_DIR / stored_name).write_bytes(data)

	with connect() as conn:
		cur = conn.execute(
			"""
			INSERT INTO documents
				(user_id, filename, stored_name, size_bytes, mime, status, week, uploaded_at)
			VALUES (?,?,?,?,?,?,?,?)
			""",
			(
				user["id"],
				filename,
				stored_name,
				len(data),
				file.content_type or "application/octet-stream",
				"queued",
				week or None,
				time.time(),
			),
		)
		row = conn.execute("SELECT * FROM documents WHERE id = ?", (cur.lastrowid,)).fetchone()

		logger.info(
			"document_record_created document_id=%s filename=%s bytes=%d status=queued",
			row["id"],
			filename,
			len(data),
		)

	asyncio.create_task(
		start_upload_pipeline(
			data,
			filename,
			max_nodes=max_nodes,
			min_mentions=min_mentions,
			document_id=row["id"],
			stored_name=stored_name,
		)
	)
	logger.info("document_pipeline_started document_id=%s filename=%s", row["id"], filename)

	return {"document": document_public(row)}


@router.patch("/api/documents/{doc_id}")
async def update_document(
	doc_id: int, body: DocumentPatch, user: sqlite3.Row = Depends(current_user)
):
	fields = {key: value for key, value in body.model_dump().items() if value is not None}
	logger.info("document_update_received document_id=%s fields=%s", doc_id, sorted(fields))
	if not fields:
		raise HTTPException(status_code=422, detail="Nothing to update.")

	assignments = ", ".join(f"{key} = ?" for key in fields)
	with connect() as conn:
		owned = conn.execute(
			"SELECT 1 FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
		).fetchone()
		if not owned:
			raise HTTPException(status_code=404, detail="That document is not in your library.")

		conn.execute(
			f"UPDATE documents SET {assignments} WHERE id = ? AND user_id = ?",
			(*fields.values(), doc_id, user["id"]),
		)
		row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

		logger.info(
			"document_update_complete document_id=%s status=%s",
			doc_id,
			row["status"],
		)

	return {"document": document_public(row)}


@router.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: int, user: sqlite3.Row = Depends(current_user)):
	logger.info("document_delete_received document_id=%s user_id=%s", doc_id, user["id"])
	with connect() as conn:
		row = conn.execute(
			"SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
		).fetchone()
		if row is None:
			raise HTTPException(status_code=404, detail="That document is not in your library.")

		conn.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"]))

	stored = UPLOADS_DIR / row["stored_name"]
	if stored.exists():
		try:
			stored.unlink()
		except OSError:
			pass

	logger.info("document_delete_complete document_id=%s", doc_id)

	return {"ok": True, "deleted": doc_id}


__all__ = ["router"]
