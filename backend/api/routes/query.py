import asyncio
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from repositories.users import SupabaseIdentity, current_supabase_identity
from schemas.query import GraphQueryRequest
from app import state as runtime_state
from services.supabase_rag import answer_graph_query

router = APIRouter(tags=["query"])


@router.post("/api/graphs/{graph_id}/query")
async def post_graph_query(
    graph_id: UUID,
    body: GraphQueryRequest,
    identity: SupabaseIdentity = Depends(current_supabase_identity),
):
    if not body.query.strip():
        raise HTTPException(status_code=422, detail="Ask a question about this graph.")
    return await asyncio.to_thread(
        answer_graph_query,
        identity,
        str(graph_id),
        body.query.strip(),
        runtime_state.client,
    )
