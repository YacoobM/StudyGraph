from fastapi import APIRouter, Depends
from uuid import UUID

from repositories.supabase_graphs import (
	create_graph,
	get_graph_active_support,
	get_graph_concept_evolution,
	list_graph_versions,
	list_graphs,
	load_graph_version,
	update_graph_name,
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
from schemas.graph import GraphCreateRequest, GraphVersionCreateRequest
from pydantic import BaseModel, field_validator
from app import state as runtime_state
from services.supabase_graph_processing import create_graph_version_from_batch
from services.supabase_concept_refresh import refresh_degraded_concept_definitions


router = APIRouter(tags=["graphs"])
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


@router.patch("/api/graphs/{graph_id}")
def patch_graph(graph_id: UUID, body: LibraryNameRequest, identity: SupabaseIdentity = Depends(current_supabase_identity)):
	return {"graph": update_graph_name(identity, str(graph_id), body.name)}


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


@router.get("/api/graphs/{graph_id}/support")
def get_graph_support(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return get_graph_active_support(identity, str(graph_id))


@router.post("/api/graphs/{graph_id}/concepts/refresh-degraded")
async def refresh_degraded_concepts(
	graph_id: UUID,
	identity: SupabaseIdentity = Depends(current_supabase_identity),
):
	return await refresh_degraded_concept_definitions(identity, str(graph_id), runtime_state.client)
