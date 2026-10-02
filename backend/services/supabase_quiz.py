from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import HTTPException

from repositories.supabase_graphs import list_graph_versions, load_graph_version, request_table
from repositories.users import SupabaseIdentity
from services.quiz_service import QuizConfig, _questions_from_graph, _questions_from_llm


async def get_graph_quiz(identity: SupabaseIdentity, graph_id: str) -> dict:
    versions = list_graph_versions(identity, graph_id)
    if not versions:
        return {"topics": [], "generated": False, "reason": "No documents have been processed yet."}
    version = versions[0]
    version_id = str(version["id"])
    existing = request_table(identity, "quiz_sets", f"?select=id,status&graph_version_id=eq.{version_id}&user_id=eq.{identity.auth_user_id}")
    if existing:
        quiz_set_id = str(existing[0]["id"])
        questions = request_table(identity, "quiz_questions", f"?select=id,question,options,correct_index,explanation,difficulty&quiz_set_id=eq.{quiz_set_id}&order=question_order.asc")
        if questions:
            attempts = request_table(identity, "quiz_attempts", f"?select=id,quiz_set_id,status,current_index,answers,correct,total,points,passed&quiz_set_id=eq.{quiz_set_id}&order=updated_at.desc&limit=1")
            attempt = attempts[0] if attempts else None
            return {"topics": [_topic(version, quiz_set_id, questions)], "generated": True, "reason": None,
                    "resume": attempt if attempt and attempt.get("status") == "in_progress" else None,
                    "result": attempt}

    loaded = load_graph_version(identity, graph_id, version_id)
    concepts = loaded["concepts"]
    edges = [{"from": edge.get("source_concept_id"), "to": edge.get("target_concept_id"), "relation": edge.get("relationship_type", "related to")} for edge in loaded["relationships"]]
    names_by_id = {str(concept["id"]): concept["name"] for concept in concepts}
    for edge in edges:
        edge["from"] = names_by_id.get(str(edge["from"]), "")
        edge["to"] = names_by_id.get(str(edge["to"]), "")
    if len(concepts) < QuizConfig.MIN_CONCEPTS_PER_TOPIC:
        return {"topics": [], "generated": False, "reason": "Not enough concepts yet — upload more material for questions."}
    names = sorted({concept["name"] for concept in concepts})
    pool = await asyncio.to_thread(_questions_from_llm, names, edges, QuizConfig.POOL_SIZE_PER_TOPIC)
    if not pool:
        pool = _questions_from_graph(concepts, edges, f"Version {version['version_number']}")
    if len(pool) < QuizConfig.MIN_POOL_TO_SHOW_TOPIC:
        return {"topics": [], "generated": False, "reason": "Not enough relationships yet for questions."}

    inserted = request_table(identity, "quiz_sets", "?select=id", method="POST", payload={"user_id": identity.auth_user_id, "graph_id": graph_id, "graph_version_id": version_id, "status": "ready"}, prefer="return=representation")
    quiz_set_id = str(inserted[0]["id"])
    rows = [{"quiz_set_id": quiz_set_id, "question_order": index, "question": item["q"], "options": item["o"], "correct_index": item["a"], "explanation": item.get("why", ""), "difficulty": item.get("d", "medium")} for index, item in enumerate(pool)]
    request_table(identity, "quiz_questions", "?select=id", method="POST", payload=rows, prefer="return=minimal")
    return {"topics": [_topic(version, quiz_set_id, [{"id": str(uuid4()), "question": row["question"], "options": row["options"], "correct_index": row["correct_index"], "explanation": row["explanation"], "difficulty": row["difficulty"]} for row in rows])], "generated": True, "reason": None, "resume": None, "result": None}


def _topic(version: dict, quiz_set_id: str, rows: list[dict]) -> dict:
    questions = [{"id": str(row["id"]), "q": row["question"], "o": row["options"], "a": row["correct_index"], "why": row["explanation"], "d": row["difficulty"]} for row in rows]
    return {"id": quiz_set_id, "day": int(version["version_number"]), "name": f"Graph Version {version['version_number']}", "desc": "Questions from the latest graph version.", "status": "ready", "questions": questions}


def save_quiz_progress(identity: SupabaseIdentity, quiz_set_id: str, current_index: int, answers: dict, correct: int, total: int, points: int, completed: bool = False) -> dict:
    if total <= 0:
        raise HTTPException(status_code=422, detail="A quiz needs at least one question.")
    passed = correct / total >= 2 / 3
    status = "completed" if completed else "in_progress"
    existing = request_table(identity, "quiz_attempts", f"?select=id&user_id=eq.{identity.auth_user_id}&quiz_set_id=eq.{quiz_set_id}&status=eq.in_progress&limit=1")
    payload = {"user_id": identity.auth_user_id, "quiz_set_id": quiz_set_id, "correct": correct, "total": total, "points": points, "passed": passed, "status": status, "current_index": current_index, "answers": answers}
    if existing:
        rows = request_table(identity, "quiz_attempts", f"?id=eq.{existing[0]['id']}", method="PATCH", payload=payload, prefer="return=representation")
    else:
        rows = request_table(identity, "quiz_attempts", "?select=id,correct,total,points,passed,status,current_index,answers,created_at", method="POST", payload=payload, prefer="return=representation")
    return {"result": rows[0], "passed": passed}
