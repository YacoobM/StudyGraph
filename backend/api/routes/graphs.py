from pathlib import Path
import asyncio
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from uuid import UUID

from repositories.supabase_documents import (
	create_graph_document,
	list_graph_documents,
	persist_document_chunks,
	set_graph_document_status,
)
from repositories.supabase_graphs import (
	create_graph,
	get_graph_active_support,
	get_graph_concept_evolution,
	list_graph_versions,
	list_graphs,
	load_graph_version,
)
from repositories.supabase_library import (
	create_subject,
	create_topic,
	delete_subject,
	delete_topic,
	list_library,
	update_subject,
	update_topic,
)
from repositories.users import SupabaseIdentity, current_supabase_identity
from services.chunking import build_document_chunks
from services.document_service import extract_text
from services.embeddings import encode_texts
from schemas.graph import GraphCreateRequest, GraphVersionCreateRequest
from pydantic import BaseModel, field_validator
from schemas.query import GraphQueryRequest
from app import state as runtime_state
from services.supabase_graph_processing import create_graph_version_from_batch
from services.supabase_graph_processing import delete_graph_document_and_create_version
from services.supabase_rag import answer_graph_query
from services.supabase_quiz import get_graph_quiz, regenerate_graph_quiz, save_quiz_progress
from services.supabase_note_quiz import save_note_quiz_progress
from repositories.supabase_documents import signed_graph_document_url
from repositories.supabase_notes import get_note as get_supabase_note, list_graph_notes, mark_graph_notes_stale, plan_graph_note, write_graph_note_section


router = APIRouter(tags=["graphs"])
MAX_GRAPH_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_GRAPH_SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md"}


class LibraryNameRequest(BaseModel):
	name: str

	@field_validator("name")
	@classmethod
	def normalize_name(cls, name: str) -> str:
		name = name.strip()
		if not name:
			raise ValueError("Name cannot be empty.")
		return name


class TopicCreateRequest(LibraryNameRequest):
	subject_id: UUID


@router.get("/api/library")
def get_library(identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"subjects": list_library(identity)}


@router.post("/api/subjects", status_code=201)
def post_subject(body: LibraryNameRequest, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"subject": create_subject(identity, body.name)}


@router.patch("/api/subjects/{subject_id}")
def patch_subject(subject_id: UUID, body: LibraryNameRequest, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"subject": update_subject(identity, str(subject_id), body.name)}


@router.delete("/api/subjects/{subject_id}", status_code=204)
def remove_subject(subject_id: UUID, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	delete_subject(identity, str(subject_id))


@router.post("/api/topics", status_code=201)
def post_topic(body: TopicCreateRequest, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"topic": create_topic(identity, str(body.subject_id), body.name)}


@router.patch("/api/topics/{topic_id}")
def patch_topic(topic_id: UUID, body: LibraryNameRequest, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"topic": update_topic(identity, str(topic_id), body.name)}


@router.delete("/api/topics/{topic_id}", status_code=204)
def remove_topic(topic_id: UUID, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	delete_topic(identity, str(topic_id))


@router.get("/api/graphs")
def get_graphs(identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"graphs": list_graphs(identity)}


@router.post("/api/graphs", status_code=201)
def post_graph(
	body: GraphCreateRequest,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return {"graph": create_graph(identity, body.name, body.topic_id)}


@router.get("/api/graphs/{graph_id}/versions")
def get_graph_versions(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return {"versions": list_graph_versions(identity, str(graph_id))}


@router.post("/api/graphs/{graph_id}/versions", status_code=201)
async def post_graph_version(
	graph_id: UUID,
	body: GraphVersionCreateRequest,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return await create_graph_version_from_batch(
		identity,
		str(graph_id),
		body.document_ids,
		runtime_state.client,
		max_nodes_per_document=body.max_nodes_per_document,
		min_mentions=body.min_mentions,
	)


@router.get("/api/graphs/{graph_id}/versions/{version_id}")
def get_graph_version(
	graph_id: UUID,
	version_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return load_graph_version(identity, str(graph_id), str(version_id))


@router.get("/api/graphs/{graph_id}/concepts/{concept_name}/evolution")
async def get_graph_concept_evolution_route(
	graph_id: UUID,
	concept_name: str,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return await get_graph_concept_evolution(identity, str(graph_id), concept_name, runtime_state.client)


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


@router.get("/api/graphs/{graph_id}/support")
def get_graph_support(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return get_graph_active_support(identity, str(graph_id))


@router.delete("/api/graphs/{graph_id}/documents/{document_id}")
async def delete_graph_document(
	graph_id: UUID,
	document_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return await delete_graph_document_and_create_version(identity, str(graph_id), str(document_id))


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
		embeddings = await asyncio.to_thread(encode_texts, [chunk["text"] for chunk in chunks])
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


@router.post("/api/graphs/{graph_id}/query")
def post_graph_query(
	graph_id: UUID,
	body: GraphQueryRequest,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	if not body.query.strip():
		raise HTTPException(status_code=422, detail="Ask a question about this graph.")
	return answer_graph_query(identity, str(graph_id), body.query.strip(), runtime_state.client)


@router.get("/api/graphs/{graph_id}/quiz")
async def get_graph_quiz_route(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return await get_graph_quiz(identity, str(graph_id))


@router.post("/api/graphs/{graph_id}/quiz/regenerate")
async def regenerate_graph_quiz_route(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return await regenerate_graph_quiz(identity, str(graph_id))


@router.get("/api/graphs/{graph_id}/notes")
def get_graph_notes_route(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	versions = list_graph_versions(identity, str(graph_id))
	if versions:
		mark_graph_notes_stale(identity, str(graph_id), str(versions[0]["id"]))
	return {"notes": list_graph_notes(identity, str(graph_id))}


@router.get("/api/graphs/{graph_id}/notes/{note_id}")
def get_graph_note_route(
	graph_id: UUID,
	note_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	note = get_supabase_note(identity, str(graph_id), str(note_id))
	if note is None:
		raise HTTPException(status_code=404, detail="Note not found")
	return {"note": note}


@router.post("/api/graphs/{graph_id}/notes/plan")
async def plan_graph_notes_route(
	graph_id: UUID,
	body: dict,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	try:
		note = await plan_graph_note(identity, str(graph_id), [str(value) for value in body.get("document_ids") or []], body.get("days"))
	except ValueError as error:
		raise HTTPException(status_code=422, detail=str(error)) from error
	return {"note": note}


@router.post("/api/graphs/{graph_id}/notes/{note_id}/sections/{section_id}/write")
async def write_graph_note_section_route(
	graph_id: UUID,
	note_id: UUID,
	section_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	result = await write_graph_note_section(identity, str(graph_id), str(note_id), str(section_id))
	if result is None:
		raise HTTPException(status_code=404, detail="Note section not found or could not be written.")
	return result


@router.post("/api/graphs/{graph_id}/quiz/results")
async def save_graph_quiz_result(
	graph_id: UUID,
	body: dict,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return save_quiz_progress(identity, str(body.get("quiz_id") or ""), int(body.get("current_index") or 0), body.get("answers") or {}, int(body.get("correct") or 0), int(body.get("total") or 0), int(body.get("points") or 0), bool(body.get("completed")))


@router.post("/api/graphs/{graph_id}/notes/sections/{section_id}/quiz-progress")
async def save_note_quiz_result(
	graph_id: UUID,
	section_id: UUID,
	body: dict,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return save_note_quiz_progress(
		identity,
		str(graph_id),
		str(section_id),
		int(body.get("current_index") or 0),
		body.get("answers") or {},
		int(body.get("correct") or 0),
		int(body.get("total") or 0),
		int(body.get("points") or 0),
		bool(body.get("completed")),
	)
