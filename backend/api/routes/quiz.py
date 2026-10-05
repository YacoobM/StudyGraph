from fastapi import APIRouter, Depends
from uuid import UUID

from repositories.users import SupabaseIdentity, current_supabase_identity
from services.supabase_quiz import get_graph_quiz, regenerate_graph_quiz, save_quiz_progress

router = APIRouter(tags=["quiz"])


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


@router.post("/api/graphs/{graph_id}/quiz/results")
async def save_graph_quiz_result(
    graph_id: UUID,
    body: dict,
    identity: SupabaseIdentity = Depends(current_supabase_identity),
):
    return save_quiz_progress(
        identity,
        str(body.get("quiz_id") or ""),
        int(body.get("current_index") or 0),
        body.get("answers") or {},
        int(body.get("correct") or 0),
        int(body.get("total") or 0),
        int(body.get("points") or 0),
        bool(body.get("completed")),
    )
