from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from repositories.supabase_graphs import (
    list_graph_versions,
)

from repositories.users import SupabaseIdentity, current_supabase_identity
from services.supabase_note_quiz import save_note_quiz_progress
from repositories.supabase_notes import (
    get_note as get_supabase_note,
    list_graph_notes,
    mark_graph_notes_stale,
    plan_graph_note,
    write_graph_note_section,
)
from app.logging_config import get_logger

router = APIRouter(tags=["notes"])
logger = get_logger(__name__)


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
        note = await plan_graph_note(
            identity,
            str(graph_id),
            [str(value) for value in body.get("document_ids") or []],
            body.get("days"),
        )
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
    result = await write_graph_note_section(
        identity, str(graph_id), str(note_id), str(section_id)
    )
    if result is None:
        raise HTTPException(
            status_code=404, detail="Note section not found or could not be written."
        )
    return result


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
