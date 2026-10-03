from fastapi import HTTPException

from repositories.supabase_graphs import request_table
from repositories.users import SupabaseIdentity


def _one(rows: list[dict], detail: str) -> dict:
	if len(rows) != 1:
		raise HTTPException(status_code=502, detail=detail)
	return rows[0]


def list_library(identity: SupabaseIdentity) -> list[dict]:
	subjects = request_table(
		identity,
		"subjects",
		"?select=id,name,created_at&order=created_at.asc,id.asc",
	)
	topics = request_table(
		identity,
		"topics",
		"?select=id,subject_id,name,created_at,updated_at&order=created_at.asc,id.asc",
	)
	graphs = request_table(
		identity,
		"graphs",
		"?select=id,topic_id,name,created_at,updated_at&order=created_at.asc,id.asc",
	)
	documents = request_table(
		identity,
		"documents",
		"?select=id,graph_id",
	)
	doc_ids_by_graph: dict[str, list[str]] = {}
	for document in documents:
		if document.get("graph_id"):
			doc_ids_by_graph.setdefault(str(document["graph_id"]), []).append(str(document["id"]))

	graphs_by_topic: dict[str, list[dict]] = {}
	for graph in graphs:
		graphs_by_topic.setdefault(str(graph.get("topic_id")), []).append({
			**graph,
			"doc_ids": doc_ids_by_graph.get(str(graph["id"]), []),
		})

	topics_by_subject: dict[str, list[dict]] = {}
	for topic in topics:
		topics_by_subject.setdefault(str(topic["subject_id"]), []).append({
			**topic,
			"graphs": graphs_by_topic.get(str(topic["id"]), []),
		})

	return [
		{
			**subject,
			"topics": topics_by_subject.get(str(subject["id"]), []),
		}
		for subject in subjects
	]


def create_subject(identity: SupabaseIdentity, name: str) -> dict:
	return _one(request_table(
		identity,
		"subjects",
		"?select=id,name,created_at",
		method="POST",
		payload={"user_id": identity.auth_user_id, "name": name},
		prefer="return=representation",
	), "Supabase did not return the created subject.")


def update_subject(identity: SupabaseIdentity, subject_id: str, name: str) -> dict:
	return _one(request_table(
		identity,
		"subjects",
		f"?id=eq.{subject_id}&select=id,name,created_at",
		method="PATCH",
		payload={"name": name},
		prefer="return=representation",
	), "Subject not found.")


def delete_subject(identity: SupabaseIdentity, subject_id: str) -> None:
	request_table(identity, "subjects", f"?id=eq.{subject_id}", method="DELETE", prefer="return=minimal")


def create_topic(identity: SupabaseIdentity, subject_id: str, name: str) -> dict:
	return _one(request_table(
		identity,
		"topics",
		"?select=id,subject_id,name,created_at,updated_at",
		method="POST",
		payload={"subject_id": subject_id, "name": name},
		prefer="return=representation",
	), "Supabase did not return the created topic.")


def update_topic(identity: SupabaseIdentity, topic_id: str, name: str) -> dict:
	return _one(request_table(
		identity,
		"topics",
		f"?id=eq.{topic_id}&select=id,subject_id,name,created_at,updated_at",
		method="PATCH",
		payload={"name": name},
		prefer="return=representation",
	), "Topic not found.")


def delete_topic(identity: SupabaseIdentity, topic_id: str) -> None:
	request_table(identity, "topics", f"?id=eq.{topic_id}", method="DELETE", prefer="return=minimal")
