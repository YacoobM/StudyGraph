from __future__ import annotations

import logging

from fastapi import HTTPException

from repositories.supabase_graphs import request_table
from repositories.supabase_graphs import list_graph_versions, load_graph_version
from repositories.supabase_documents import list_graph_documents
from repositories.users import SupabaseIdentity
from services.notes_service import request_titles
from services.notes_service import write_body
from repositories.supabase_search import search_graph_chunks
from services.supabase_note_quiz import latest_note_quiz_progress
from services.note_days import assign_days, repair_note_days, split_sections_for_days

logger = logging.getLogger(__name__)


def _fallback_section_title(filename: str) -> str:
    stem = str(filename or "Mixed sources").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return " ".join(stem.replace("_", " ").replace("-", " ").split()) or "Mixed sources"


async def _plan_supabase_sections(concepts: list[dict], cache_key: str, days: int | None = None) -> list[dict]:
    """Build note sections from graph provenance: one per source document, in page order.

    For a day plan, large sections are split so each requested day can get one.
    """
    by_document: dict[str, list[dict]] = {}
    for concept in concepts:
        by_document.setdefault(concept.get("source") or "Mixed sources", []).append(concept)

    sections = []
    for filename, rows in by_document.items():
        # First page a concept appears on, then first mention, keeps reading order.
        rows = sorted(enumerate(rows), key=lambda item: (item[1].get("page") or 10**6, item[0]))
        names = list(dict.fromkeys(row["name"] for _, row in rows))
        sections.append({"title": "", "concepts": names, "document": filename, "range": (0, 1)})
    sections = split_sections_for_days(sections, days)
    if not sections:
        return []

    titles = await request_titles(sections, cache_key)
    parts: dict[str, int] = {}
    per_document = {s["document"]: sum(1 for x in sections if x["document"] == s["document"]) for s in sections}
    for index, section in enumerate(sections):
        parts[section["document"]] = parts.get(section["document"], 0) + 1
        fallback = _fallback_section_title(section["document"])
        if per_document[section["document"]] > 1:
            fallback = f"{fallback} (part {parts[section['document']]})"
        section["title"] = (
            titles[index].strip()
            if titles and index < len(titles) and titles[index].strip()
            else fallback
        )
    return sections


def _sections(identity: SupabaseIdentity, note_id: str) -> list[dict]:
    return request_table(
        identity,
        "note_sections",
        f"?select=id,section_order,day,title,concepts,relations,sources,must_cover,body,done,created_at,updated_at"
        f"&note_id=eq.{note_id}&order=section_order.asc",
    )


def _with_sections(identity: SupabaseIdentity, rows: list[dict]) -> list[dict]:
    for row in rows:
        row["sections"] = _sections(identity, str(row["id"]))
        for section in row["sections"]:
            section["quiz_progress"] = latest_note_quiz_progress(identity, str(section["id"]))
        row["total"] = len(row["sections"])
        repair_note_days(row)
    return rows


def list_graph_notes(identity: SupabaseIdentity, graph_id: str) -> list[dict]:
    rows = request_table(
        identity,
        "notes",
        f"?select=id,graph_id,graph_version_id,title,kind,days,status,document_ids,documents,created_at,updated_at"
        f"&graph_id=eq.{graph_id}&user_id=eq.{identity.auth_user_id}&order=updated_at.desc",
    )
    return _with_sections(identity, rows)


def get_note(identity: SupabaseIdentity, graph_id: str, note_id: str) -> dict | None:
    rows = request_table(
        identity,
        "notes",
        f"?select=id,graph_id,graph_version_id,title,kind,days,status,document_ids,documents,created_at,updated_at"
        f"&id=eq.{note_id}&graph_id=eq.{graph_id}&user_id=eq.{identity.auth_user_id}",
    )
    return _with_sections(identity, rows)[0] if rows else None


def mark_graph_notes_stale(identity: SupabaseIdentity, graph_id: str, latest_version_id: str) -> None:
    request_table(
        identity,
        "notes",
        f"?graph_id=eq.{graph_id}&user_id=eq.{identity.auth_user_id}&graph_version_id=neq.{latest_version_id}",
        method="PATCH",
        payload={"status": "stale"},
        prefer="return=minimal",
    )


def replace_graph_note(
    identity: SupabaseIdentity,
    graph_id: str,
    graph_version_id: str,
    note: dict,
    sections: list[dict],
) -> dict:
    existing = request_table(
        identity,
        "notes",
        f"?select=id&graph_id=eq.{graph_id}&user_id=eq.{identity.auth_user_id}&limit=1",
    )
    if existing:
        note_id = str(existing[0]["id"])
        request_table(identity, "notes", f"?id=eq.{note_id}", method="PATCH", payload={**note, "graph_version_id": graph_version_id, "status": "current"}, prefer="return=minimal")
        request_table(identity, "note_sections", f"?note_id=eq.{note_id}", method="DELETE", prefer="return=minimal")
    else:
        rows = request_table(
            identity,
            "notes",
            "?select=id",
            method="POST",
            payload={**note, "user_id": identity.auth_user_id, "graph_id": graph_id, "graph_version_id": graph_version_id, "status": "current"},
            prefer="return=representation",
        )
        note_id = str(rows[0]["id"])
    section_rows = [
        {
            **section,
            "note_id": note_id,
            "section_order": index,
        }
        for index, section in enumerate(sections)
    ]
    if section_rows:
        request_table(identity, "note_sections", "?select=id", method="POST", payload=section_rows, prefer="return=minimal")
    return get_note(identity, graph_id, note_id)


def update_note_section(identity: SupabaseIdentity, note_id: str, section_id: str, payload: dict) -> dict | None:
    rows = request_table(
        identity,
        "note_sections",
        f"?id=eq.{section_id}&note_id=eq.{note_id}",
        method="PATCH",
        payload=payload,
        prefer="return=representation",
    )
    return rows[0] if rows else None


async def plan_graph_note(identity: SupabaseIdentity, graph_id: str, document_ids: list[str], days: int | None = None) -> dict:
    """Create a note plan from selected documents' provenance in the latest sealed graph version."""
    versions = list_graph_versions(identity, graph_id)
    if not versions:
        raise ValueError("This graph has no sealed version yet.")
    version = versions[0]
    loaded = load_graph_version(identity, graph_id, str(version["id"]))
    documents = {str(row["id"]): row for row in list_graph_documents(identity, graph_id)}
    selected = {str(value) for value in document_ids}
    if not selected or not selected.issubset(documents):
        raise ValueError("Select documents belonging to this graph.")
    names_by_id = {str(row["id"]): row["name"] for row in loaded["concepts"]}
    provenance = loaded.get("concept_provenance") or []
    concepts = []
    seen = set()
    for row in provenance:
        if str(row.get("document_id")) not in selected:
            continue
        name = names_by_id.get(str(row.get("concept_id")))
        key = (name, str(row.get("document_id")))
        if name and key not in seen:
            seen.add(key)
            concepts.append({
                "name": name,
                "source": documents[str(row["document_id"])]["filename"],
                "week": f"Week {row.get('source_week') or 1}",
                "page": row.get("page_start"),
            })
    names = {row["name"] for row in concepts}
    edges = []
    for edge in loaded["relationships"]:
        source = names_by_id.get(str(edge.get("source_concept_id")))
        target = names_by_id.get(str(edge.get("target_concept_id")))
        if source in names and target in names:
            edges.append({"from": source, "to": target, "relation": edge.get("relationship_type", "related_to"), "summary": edge.get("summary", "")})
    if not concepts:
        raise ValueError("No extracted concepts found for those documents.")
    days = int(days) if days else None
    sections = await _plan_supabase_sections(
        concepts,
        f"supabase-notes:{version['id']}:{','.join(sorted(selected))}:days={days or 0}",
        days,
    )
    if not sections:
        raise ValueError("The AI Service returned an unusable plan.")
    sections, used_days = assign_days(sections, days)
    note_sections = [
        {
            "day": section["day"],
            "title": section["title"],
            "concepts": section.get("concepts", []),
            "relations": [
                edge for edge in edges
                if edge.get("from") in section.get("concepts", []) and edge.get("to") in section.get("concepts", [])
            ],
            "sources": sorted({row["source"] for row in concepts if row["name"] in section.get("concepts", [])}),
            "must_cover": section.get("must_cover", []),
            "body": None,
            "done": False,
        }
        for section in sections
    ]
    note = {
        "title": "Revision notes" if len(selected) > 1 else documents[next(iter(selected))]["filename"],
        # Fewer sections than days shortens the plan instead of leaving empty days.
        "kind": "plan" if used_days else "full",
        "days": used_days,
        "document_ids": sorted(selected),
        "documents": sorted(documents[value]["filename"] for value in selected),
    }
    return replace_graph_note(identity, graph_id, str(version["id"]), note, note_sections)


async def write_graph_note_section(identity: SupabaseIdentity, graph_id: str, note_id: str, section_id: str) -> dict | None:
    note = get_note(identity, graph_id, note_id)
    if not note:
        return None
    section = next((row for row in note["sections"] if str(row["id"]) == str(section_id)), None)
    if not section:
        return None
    if section.get("body") is not None:
        logger.info(
            "notes_section_cached graph=%s note=%s section=%s source=supabase model_call=false",
            graph_id,
            note_id,
            section_id,
        )
        return {"section": section, "generated": False, "retry_after": None}
    logger.info(
        "notes_section_generating graph=%s note=%s section=%s source=supabase model_call=true",
        graph_id,
        note_id,
        section_id,
    )
    other_concepts = [concept for row in note["sections"] if row is not section for concept in (row.get("concepts") or [])]
    documents = set(note.get("documents") or [])

    def retrieve(concept: str, top_k: int = 3):
        hits = search_graph_chunks(identity, graph_id, concept, top_k=top_k)
        return [hit for hit in hits if not documents or hit.get("source") in documents]

    body, retry_after = await write_body(section, documents, other_concepts, chunk_retriever=retrieve)
    if body is None:
        raise HTTPException(status_code=502, detail="Note writing failed. Check the configured GEMINI_API_KEY and try again.")
    updated = update_note_section(identity, note_id, section_id, {"body": body})
    return {"section": updated, "generated": True, "retry_after": retry_after}
