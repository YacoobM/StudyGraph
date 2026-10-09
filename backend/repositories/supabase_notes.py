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

logger = logging.getLogger(__name__)


def _fallback_section_title(filename: str) -> str:
    stem = str(filename or "Mixed sources").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return " ".join(stem.replace("_", " ").replace("-", " ").split()) or "Mixed sources"


async def _plan_supabase_sections(concepts: list[dict], edges: list[dict], cache_key: str) -> list[dict]:
    """Build Supabase note sections from graph provenance.

    The current notes service plans SQLite notes from document text internally;
    it no longer exposes the old ``plan_sections`` graph-only helper. Supabase
    already has the graph snapshot and provenance, so group concepts by their
    source document here and reuse the current service's title call.
    """
    by_document: dict[str, list[str]] = {}
    for concept in concepts:
        filename = concept.get("source") or "Mixed sources"
        by_document.setdefault(filename, []).append(concept["name"])

    sections = [
        {
            "title": "",
            "concepts": sorted(set(names)),
            "document": filename,
            "range": (0, 1),
        }
        for filename, names in by_document.items()
    ]
    if not sections:
        return []

    titles = await request_titles(sections, cache_key)
    for index, section in enumerate(sections):
        section["title"] = (
            titles[index].strip()
            if titles and index < len(titles) and titles[index].strip()
            else _fallback_section_title(section["document"])
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
            concepts.append({"name": name, "source": documents[str(row["document_id"])] ["filename"], "week": f"Week {row.get('source_week') or 1}"})
    names = {row["name"] for row in concepts}
    edges = []
    for edge in loaded["relationships"]:
        source = names_by_id.get(str(edge.get("source_concept_id")))
        target = names_by_id.get(str(edge.get("target_concept_id")))
        if source in names and target in names:
            edges.append({"from": source, "to": target, "relation": edge.get("relationship_type", "related_to"), "summary": edge.get("summary", "")})
    if not concepts:
        raise ValueError("No extracted concepts found for those documents.")
    sections = await _plan_supabase_sections(
        concepts,
        edges,
        f"supabase-notes:{version['id']}:{','.join(sorted(selected))}",
    )
    if not sections:
        raise ValueError("The AI Service returned an unusable plan.")
    note_sections = [{"day": section.get("day"), "title": section["title"], "concepts": section.get("concepts", []), "relations": [edge for edge in edges if edge.get("from") in section.get("concepts", []) and edge.get("to") in section.get("concepts", [])], "sources": sorted({row["source"] for row in concepts if row["name"] in section.get("concepts", [])}), "must_cover": section.get("must_cover", []), "body": None, "done": False} for section in sections]
    note = {"title": "Revision notes" if len(selected) > 1 else next(iter(documents.values()))["filename"], "kind": "plan" if days else "full", "days": days, "document_ids": sorted(selected), "documents": sorted(documents[value]["filename"] for value in selected)}
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
