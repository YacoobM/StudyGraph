from __future__ import annotations

from fastapi import HTTPException

from repositories.supabase_graphs import request_table
from repositories.users import SupabaseIdentity


def save_note_quiz_progress(
    identity: SupabaseIdentity,
    graph_id: str,
    section_id: str,
    current_index: int,
    answers: dict,
    correct: int,
    total: int,
    points: int,
    completed: bool = False,
) -> dict:
    if total <= 0:
        raise HTTPException(status_code=422, detail="A quiz needs at least one question.")

    sections = request_table(
        identity,
        "note_sections",
        f"?select=id,note_id&id=eq.{section_id}",
    )
    if not sections:
        raise HTTPException(status_code=404, detail="Note quiz section not found.")
    note_id = str(sections[0]["note_id"])
    notes = request_table(
        identity,
        "notes",
        f"?select=id&graph_id=eq.{graph_id}&user_id=eq.{identity.auth_user_id}&id=eq.{note_id}",
    )
    if not notes:
        raise HTTPException(status_code=404, detail="Note does not belong to this graph.")

    passed = correct / total >= 2 / 3
    status = "completed" if completed else "in_progress"
    payload = {
        "user_id": identity.auth_user_id,
        "graph_id": graph_id,
        "note_id": note_id,
        "section_id": section_id,
        "status": status,
        "current_index": current_index,
        "answers": answers,
        "correct": correct,
        "total": total,
        "points": points,
        "passed": passed,
    }
    existing = request_table(
        identity,
        "note_quiz_attempts",
        f"?select=id&user_id=eq.{identity.auth_user_id}&section_id=eq.{section_id}&status=eq.in_progress&limit=1",
    )
    if existing:
        rows = request_table(
            identity,
            "note_quiz_attempts",
            f"?id=eq.{existing[0]['id']}",
            method="PATCH",
            payload=payload,
            prefer="return=representation",
        )
    else:
        rows = request_table(
            identity,
            "note_quiz_attempts",
            "?select=id,section_id,status,current_index,answers,correct,total,points,passed,created_at,updated_at",
            method="POST",
            payload=payload,
            prefer="return=representation",
        )
    return {"result": rows[0], "passed": passed}


def latest_note_quiz_progress(identity: SupabaseIdentity, section_id: str) -> dict | None:
    rows = request_table(
        identity,
        "note_quiz_attempts",
        f"?select=id,section_id,status,current_index,answers,correct,total,points,passed,created_at,updated_at"
        f"&user_id=eq.{identity.auth_user_id}&section_id=eq.{section_id}&order=updated_at.desc&limit=1",
    )
    return rows[0] if rows else None
