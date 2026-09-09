"""
Revision-note planning from the extracted concept graph.

    POST /api/notes/plan     group a set of documents' concepts into sections

Mount alongside the other routers:

    from api.routes.notes import router as notes_router, set_graph_source as set_notes_graph
    app.include_router(notes_router)
    set_notes_graph(lambda: runtime_state.engine)

Only the *plan* step lives here. Planning decides what the sections are and
what each one covers; it does not write any prose. Section bodies stay empty
and the write step is deliberately not implemented yet.

Storage is a module-level dict keyed by user id, exactly as quiz_service holds
_QUIZ_CACHE and _RESULTS. Everything the graph knows lives in one in-process
engine that is lost on restart, so a note referencing it is session-scoped
too — persisting notes while the graph they point at evaporates would be worse
than not persisting them. Notes and sections are flat dicts with integer ids
and every read and write goes through the four functions under "store", so
moving to SQLite alongside quizzes later means changing those and nothing else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from api.routes.auth import current_user
from app.config import OUTPUT_DIR
from repositories.database import connect
from services import evolution_data

router = APIRouter(tags=["notes"])

# Sections the LLM is asked for. Below 4 a note is not worth splitting; above 7
# the sections get too thin to write a body from. Left to the bare range the
# model picks the minimum, which on the network-flow graph produced a single
# 14-concept section covering two distinct units, so build_plan_prompt computes
# a range per call rather than leaving it to choose the floor.
MIN_SECTIONS = 4
MAX_SECTIONS = 7

# A section is what one sitting covers and what the write step turns into one
# body. Past roughly this many concepts it stops being a unit and becomes a
# list. MAX_SECTIONS * MAX_SECTION_CONCEPTS must stay above
# _MAX_PROMPT_CONCEPTS below, or the two instructions cannot both be satisfied.
MAX_SECTION_CONCEPTS = 8

# The other end of the same idea: a section of one concept is a bullet, not a
# study unit. Two per section is the floor, so MIN_SECTIONS sections only
# become possible once there are MIN_CONCEPTS concepts to fill them. Below that
# the requested section count scales down — four concepts give two sections,
# not four sections of one, and not an error.
MIN_SECTION_CONCEPTS = 2
MIN_CONCEPTS = MIN_SECTIONS * MIN_SECTION_CONCEPTS

# Prompt budget. The plan prompt carries concept names and relation pairs only,
# never source text, and these caps keep it near 1000 input tokens. When there
# is more material than this, the highest-degree concepts and the constraints
# between them are kept — they carry the most structure.
_MAX_PROMPT_CONCEPTS = 50
_MAX_PROMPT_ORDERING = 30
_MAX_PROMPT_COHESION = 20

# Planning is recursive. A set that fits _MAX_PROMPT_CONCEPTS is grouped into
# sections in one call; anything larger is first divided into coarse parts that
# do fit, and each part is planned by the same function. Depth therefore falls
# out of the concept count instead of needing a rule per size.
#
# Parts are bounded rather than sized to exactly one call each, so a very large
# set divides again instead of asking for forty parts at once. With these two
# numbers the reachable scope is _MAX_PROMPT_CONCEPTS * _MAX_DIVIDE_PARTS **
# _MAX_PLAN_DEPTH, far past anything a student uploads — the depth cap is a
# guard against a model that will not shrink the set, not a scope limit.
_MAX_DIVIDE_PARTS = MAX_SECTIONS
_MAX_PLAN_DEPTH = 3

# --- write step -------------------------------------------------------------
# Writing a section runs when the reader opens it, not when the note is made,
# so most sections are never written at all.
#
# Its input is the source text behind the section's concepts, which runs to
# several thousand tokens — an order more than planning, which sends names and
# relation pairs only. That is why this call goes to Gemini while planning
# stays on Groq.
_CHUNKS_PER_CONCEPT = 3        # retrieve_chunks' own default
_MAX_WRITE_CHUNKS = 30         # bound on a degraded section holding many concepts
_WRITE_TIMEOUT_SECONDS = 120   # a longer prompt than planning, and a longer answer

# Planning inherits retry and backoff from evolution_data._call_with_retry.
# This call does not go through that helper, so it needs its own: Gemini
# answers a busy model with 503 UNAVAILABLE, and the first version gave up on
# it, turning a transient spike into a failed section. Waits 2s, 6s, 18s.
_WRITE_MAX_ATTEMPTS = 4
_WRITE_RETRY_BASE_SECONDS = 2

# Ceiling on the whole grouping call, retries included.
#
# evolution_data._call_with_retry is budgeted for the background upload
# pipeline, where waiting is free: 40 attempts backing off up to 60s each can
# keep trying for the better part of an hour. Planning happens while somebody
# waits on an HTTP response, so a throttled key has to become a 503 quickly
# rather than holding the request open. quiz_service caps its own call at 45s
# for the same reason.
_PLAN_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# graph source
# ---------------------------------------------------------------------------

# The app registers a callable returning the live engine, so this module never
# imports main.py (which would be circular).
_graph_source: Optional[Callable] = None


def set_graph_source(fn: Callable) -> None:
    global _graph_source
    _graph_source = fn


def _engine():
    """The live engine itself, for the write step's chunk retrieval. None when
    nothing has been uploaded in this process."""
    return _graph_source() if _graph_source else None


def _graph():
    """
    Returns (concepts, edges, canonical_nodes), or empty values when nothing
    is loaded.

    Unlike quiz_service._graph this prefers `fused_edges`, because a plan wants
    the merged graph: fusion is what resolves duplicate concept names and fixes
    edge direction. `canonical_nodes` comes back with it so concept names can
    be mapped onto the same vocabulary the fused edges already use. getattr
    defaults keep this working against dev_server's StubEngine, which has
    neither attribute.
    """
    if _graph_source is None:
        return [], [], {}
    engine = _graph_source()
    if engine is None:
        return [], [], {}
    concepts = list(getattr(engine, "concepts", []) or [])
    fused = list(getattr(engine, "fused_edges", []) or [])
    edges = fused or list(getattr(engine, "edges", []) or [])
    canonical = dict(getattr(engine, "canonical_nodes", {}) or {})
    return concepts, edges, canonical


# ---------------------------------------------------------------------------
# store
#
# Swap these four for SQLite and nothing above them changes. Notes and sections
# are flat dicts with integer ids for the same reason — they are already rows.
# ---------------------------------------------------------------------------

_NOTES: dict[int, list[dict]] = {}      # user_id -> [note]
_SEQ = {"note": 0, "section": 0}


def _next_id(kind: str) -> int:
    _SEQ[kind] += 1
    return _SEQ[kind]


def _save_note(user_id: int, note: dict) -> dict:
    _NOTES.setdefault(user_id, []).insert(0, note)   # newest first
    return note


def _load_notes(user_id: int) -> list[dict]:
    return list(_NOTES.get(user_id, []))


def _load_note(user_id: int, note_id: int) -> Optional[dict]:
    for note in _NOTES.get(user_id, []):
        if note["id"] == note_id:
            return note
    return None


def _load_section(user_id: int, section_id: int):
    """The (note, section) a section id belongs to, or (None, None). Scoped to
    the user, so one reader cannot write another's note."""
    for note in _NOTES.get(user_id, []):
        for section in note["sections"]:
            if section["id"] == section_id:
                return note, section
    return None, None


def _note_public(note: dict) -> dict:
    """Serialiser, in the shape of repositories.documents.document_public."""
    return {
        "id": note["id"],
        "title": note["title"],
        "kind": note["kind"],
        "days": note["days"],
        "document_ids": note["document_ids"],
        "documents": note["documents"],
        "created_at": note["created_at"],
        "total": len(note["sections"]),
        "done": sum(1 for s in note["sections"] if s["done"]),
        "sections": [_section_public(s) for s in note["sections"]],
    }


def _section_public(section: dict) -> dict:
    return {
        "id": section["id"],
        "note_id": section["note_id"],
        "index": section["index"],
        "day": section["day"],
        "title": section["title"],
        "concepts": section["concepts"],
        "relations": section["relations"],
        "sources": section["sources"],
        "body": section["body"],
        "done": section["done"],
    }


# ---------------------------------------------------------------------------
# relation classifier
#
# Ordering constraints ultimately come from fusion's `broader` answer, but that
# field does not survive onto an edge: graph_fusion._orient_relation consumes it
# at merge time to decide which way the edge points, then drops it. So on a
# taxonomic edge the *direction* is the broader signal, already corrected, and
# reading it back means knowing which way each label runs. The two hierarchy
# sets below mirror graph_fusion._is_hierarchical for exactly that reason.
#
# Everything else is matched tolerantly rather than looked up. The fusion prompt
# only asks for "a 1-3 word active directed verb phrase" and never enumerates
# the options, so the vocabulary is open: real graphs contain part_of,
# related_to, enables, prerequisite, applied_to, solved_by and whatever else the
# model produced that day. Normalisation happens here rather than being taken on
# trust from graph_fusion, because a graph can predate its underscore handling
# and because these labels also arrive straight off unfused engine.edges.
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_SEPARATORS = re.compile(r"[_\-‐‑‒–—−]+")


def _normalise_relation(rel: str) -> str:
    """Lowercase, separators to spaces, collapse whitespace."""
    text = _SEPARATORS.sub(" ", str(rel or "").lower())
    return _WS.sub(" ", text).strip()


# Same set graph_fusion._STRIP_CHARS removes, and for the same reason: quote
# characters are the ones a model swaps freely. Brackets, ampersands and + . /
# are left alone there and here, so C++, .NET and O(n) survive intact.
_STRIP_CHARS = re.compile(r"[,;:\"'`‘’“”]")


def _fold_diacritics(text: str) -> str:
    """Strip combining marks, so schrödinger matches Schrodinger. NFD rather
    than NFKD: the compatibility forms would also rewrite ﬁ and ①."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)
    )


def _name_key(name: str) -> str:
    """
    Matching form of a concept name, for reading the model's answer back.

    Asked to copy a name exactly the model mostly does, but it re-types the
    punctuation. The corpus already holds laplace's demon and newton's second
    law, and an echo with a typographic apostrophe is a different string, so
    an exact comparison drops the concept as invented and the neighbour
    fallback silently re-places it somewhere else.

    Four normalisations, narrowest first: NFC so a decomposed accent equals its
    precomposed form; quotes and apostrophes removed; combining marks folded;
    then the separator and whitespace handling relations already use.

    Deliberately NOT handled, because nothing has produced them yet: bracket
    style, ampersand spelling, and separators removed rather than replaced
    (mincut for min-cut).
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = _fold_diacritics(_STRIP_CHARS.sub("", text).lower())
    return _WS.sub(" ", _SEPARATORS.sub(" ", text)).strip()


# Needles are matched on word boundaries, and a trailing '*' matches any
# suffix on that final word, which is how one entry covers a verb's
# inflections. Plain containment is not usable here: "causes" contains "use",
# which silently read a causal edge as a dependency and reversed it.
#
# "A <label> B" where A is the broader concept: broader first, so A before B.
_BROADER_FIRST = ("generalis*", "generaliz*", "subsum*", "encompass*",
                  "underli*", "include*", "contain*", "comprise*",
                  "consists of", "consist of", "has part", "cover*")
# "A <label> B" where B is the broader concept: broader first means B before A.
_NARROWER_FIRST = ("is a", "is an", "type of", "is type", "has type", "kind of",
                   "form of", "subtype*", "special case", "instance of",
                   "specialis*", "specializ*", "part of", "belongs to",
                   "belong to", "member of", "component of", "subset of",
                   "example of")

# A bare copula is the same taxonomic claim as "is a" with the article left
# out — "determinism is philosophy". Matched on the whole label rather than as
# a needle: "is" as a substring would also capture "is opposite of" and
# "is split", which are not taxonomic at all.
_COPULA = frozenset({"is", "are", "was", "were"})

# "A <label> B" where A must be understood first.
# "begins with", "starts with" and "feed*" were dropped: they came from
# dev_server's invented respiration sample rather than from any real
# extraction, "feed*" would also fire on "feedback", and neither reads as
# ordinary vocabulary for a teaching order. "produce*", "yield*" and
# "establish*" appear in that sample too but stand on their own as causal
# verbs, so they stay.
_FORWARD = ("prerequisite*", "precede*", "comes before", "before", "enable*",
            "allow*", "leads to", "lead to", "cause*", "results in",
            "result in", "produce*", "yield*", "motivate*", "introduce*",
            "define*", "establish*", "applied to", "apply to")
# "A <label> B" where B must be understood first — a dependency, read backwards.
_REVERSE = ("require*", "depends on", "depend on", "relies on", "rely on",
            "need*", "presuppose*", "assume*", "builds on", "build on",
            "extend*", "based on", "derived from", "use*", "operates on",
            "operate on", "follows from")

# Symmetric or information-free: these say "these belong together" and nothing
# about order. Mirrors graph_fusion._VAGUE_RELATIONS, plus the comparison verbs.
#
# "explain*" sits here rather than in _FORWARD despite being directional. On the
# network-flow graph three of four ordering violations came from explains edges,
# and in every one the model's placement was the better teaching order: the
# min-cut max-flow theorem explains the cut, but a student meets the cut first.
# The direction is real, it just is not a learning order. Its passive form is
# handled by the inversion below, which maps cohesion onto itself.
_SYMMETRIC = ("relate*", "associated with", "connected to", "similar to",
              "same as", "equivalent to", "compar*", "contrast*", "differ*",
              "distinguish*", "alternative to", "complement*", "explain*")


_INVERSE = {"forward": "reverse", "reverse": "forward", "cohesion": "cohesion"}


def _match(label: str, needles: tuple[str, ...]) -> bool:
    """
    Whole-word phrase match. A trailing '*' drops the closing boundary so one
    needle covers a verb's inflections ('use*' catches use, uses, used) without
    matching it inside an unrelated word ('causes').
    """
    for needle in needles:
        if needle.endswith("*"):
            pattern = rf"\b{re.escape(needle[:-1])}"
        else:
            pattern = rf"\b{re.escape(needle)}\b"
        if re.search(pattern, label):
            return True
    return False


def _classify(relation: str) -> tuple[str, bool]:
    """
    Returns (kind, recognised).

    `recognised` distinguishes a label the vocabulary actually placed from one
    that merely defaulted. Both can end up "cohesion", and telling them apart
    is the only way to notice that a corpus's verbs are unknown to this list —
    otherwise a run with no ordering at all looks exactly like a run whose
    relations are genuinely all symmetric.
    """
    label = _normalise_relation(relation)
    if not label:
        return "cohesion", False

    # Passive forms are the active verb with the endpoints swapped: "A used by
    # B" is "B uses A". Strip the "by" and invert whatever the core classifies
    # as, rather than maintaining a second list that has to stay in step — and
    # do it before the containment checks, which would otherwise see "use"
    # inside "used by" and get the direction exactly backwards.
    if label.endswith(" by"):
        core = re.sub(r"^(?:is|are|was|were|be|been|being)\s+", "", label[:-3]).strip()
        if not core:
            return "cohesion", False
        kind, recognised = _classify(core)
        return _INVERSE[kind], recognised

    if label in _COPULA:
        return "reverse", True
    # Symmetric labels next: "similar to" must not be read as a dependency.
    if _match(label, _SYMMETRIC):
        return "cohesion", True
    # Then hierarchy, because it is the signal fusion actually corrected.
    if _match(label, _NARROWER_FIRST):
        return "reverse", True
    if _match(label, _BROADER_FIRST):
        return "forward", True
    if _match(label, _REVERSE):
        return "reverse", True
    if _match(label, _FORWARD):
        return "forward", True
    return "cohesion", False


def classify_relation(relation: str) -> str:
    """
    'forward'  — the edge's own direction is teaching order (A before B)
    'reverse'  — teaching order runs against the edge (B before A)
    'cohesion' — no order, but the pair belongs together

    Anything that cannot be placed confidently falls to cohesion rather than
    inventing an order. Ordering from an arbitrary verb produces exactly the
    brittle chain that cohesion hints exist to soften.
    """
    return _classify(relation)[0]


def unrecognised_labels(edges: list[dict]) -> dict[str, int]:
    """
    Relation labels no entry in the vocabulary matched, and how often each
    occurred. These all became cohesion hints by default rather than by
    judgement, so a large count here means the plan was built with little or
    no sense of order.
    """
    counts: Counter = Counter()
    for e in edges:
        if not _classify(e.get("relation"))[1]:
            counts[_normalise_relation(e.get("relation")) or "(blank)"] += 1
    return dict(counts.most_common())


def derive_constraints(edges: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """
    Splits edges into ordering constraints [(before, after)] and cohesion hints
    [(a, b)].

    A pair constrained in both directions is contradictory — the graph says A
    before B and B before A — so it is demoted to a cohesion hint rather than
    handed to the model as an impossible instruction.
    """
    ordering: set[tuple[str, str]] = set()
    cohesion: set[tuple[str, str]] = set()

    for e in edges:
        a, b = e.get("from"), e.get("to")
        if not a or not b or a == b:
            continue
        kind = classify_relation(e.get("relation"))
        if kind == "forward":
            ordering.add((a, b))
        elif kind == "reverse":
            ordering.add((b, a))
        else:
            cohesion.add(tuple(sorted((a, b))))

    contradictory = {(a, b) for (a, b) in ordering if (b, a) in ordering}
    for a, b in contradictory:
        cohesion.add(tuple(sorted((a, b))))
    ordering -= contradictory

    # Cohesion between two concepts already ordered would only muddy the
    # instruction, so ordering wins where both apply.
    ordered_pairs = {tuple(sorted(p)) for p in ordering}
    cohesion -= ordered_pairs

    return sorted(ordering), sorted(cohesion)


# ---------------------------------------------------------------------------
# graph filtering
# ---------------------------------------------------------------------------

def filter_graph_to_documents(filenames: set[str]):
    """
    Concepts grouped by the document they came from, plus the edges that
    survived, following quiz_service.document_stats: collect the names each
    source contributed, then keep an edge only when both of its endpoints are
    in the surviving set.

    Names are mapped through canonical_nodes first. Fused edges already speak
    canonical names, so without that mapping every edge would be dropped as
    having an unknown endpoint.
    """
    concepts, edges, canonical = _graph()

    def canon(name):
        return canonical.get(name, name)

    names_by_source: dict[str, set] = defaultdict(set)
    sources_by_concept: dict[str, set] = defaultdict(set)
    for c in concepts:
        source = c.get("source")
        if source not in filenames:
            continue
        name = canon(c.get("name"))
        if not name:
            continue
        names_by_source[source].add(name)
        sources_by_concept[name].add(source)

    kept = set(sources_by_concept)

    seen, kept_edges = set(), []
    for e in edges:
        a, b = canon(e.get("from")), canon(e.get("to"))
        if a not in kept or b not in kept or a == b:
            continue
        relation = str(e.get("relation") or "").strip()
        key = (a, b, relation)
        if key in seen:
            continue
        seen.add(key)
        kept_edges.append({"from": a, "relation": relation, "to": b})

    return names_by_source, sources_by_concept, kept_edges


def _degree(edges: list[dict]) -> dict[str, int]:
    degree: dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
    return degree


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

_PLAN_PROMPT_VERSION = "notes-plan-v1"
_DIVIDE_PROMPT_VERSION = "notes-divide-v1"

_DIVIDE_SYSTEM = (
    "You split a list of concepts from a student's lecture material into a few "
    "coarse parts, so that each part can be planned in detail separately. This "
    "is not the final grouping — it only has to be a sensible first cut.\n"
    "Rules:\n"
    "- Use ONLY the concept names given, copied exactly. Invent nothing.\n"
    "- Every concept must appear in exactly one part.\n"
    "- Make the parts roughly equal in size. A part far larger than the others "
    "defeats the purpose of splitting at all.\n"
    "- Keep concepts belonging to the same topic in the same part, so each part "
    "can be taught on its own.\n"
    "- Order the parts from foundations to consequences.\n"
    "- A part title is a short noun phrase naming the area it covers.\n"
    'Respond ONLY as JSON: {"sections": [{"title": "...", "concepts": ["...", "..."]}]}.'
)


def section_range(n: int) -> tuple[int, int]:
    """
    How many sections to ask for, given `n` concepts.

    Both ends fall out of the per-section size limits rather than being fixed:
    at most MAX_SECTION_CONCEPTS per section sets the floor on the count, at
    least MIN_SECTION_CONCEPTS per section sets the ceiling, and MIN_SECTIONS
    applies only where there are enough concepts to fill that many sections.

    That is what makes the small end behave. Below MIN_CONCEPTS the ceiling
    binds and the count scales down — 4 concepts give 2 sections rather than
    MIN_SECTIONS sections holding one concept each.
    """
    most = min(MAX_SECTIONS, max(1, n // MIN_SECTION_CONCEPTS))
    least = min(most, max(MIN_SECTIONS, -(-n // MAX_SECTION_CONCEPTS)))
    return least, most


def build_divide_prompt(concept_names, parts_wanted: int) -> str:
    """
    The user half of the divide prompt.

    Names only — no ordering or cohesion lists. Those are what make the
    grouping prompt expensive, and a first cut does not need them; the detailed
    constraints are applied when each part is planned. That is what lets this
    call carry several times more concepts than a grouping call.
    """
    names = sorted(concept_names)
    return (
        f"{len(names)} concepts to split into exactly {parts_wanted} parts of "
        f"roughly {-(-len(names) // parts_wanted)} concepts each.\n\n"
        "CONCEPTS:\n" + ", ".join(names)
    )

_PLAN_SYSTEM = (
    "You group concepts extracted from a student's own lecture material into "
    "the sections of a revision note. You are given concept names and the "
    "relationships between them, never the source text.\n"
    "The request states how many sections to produce; follow it, and aim for "
    "the middle of the range it gives rather than its floor. Rules:\n"
    "- Use ONLY the concept names given, copied exactly. Invent nothing.\n"
    "- Every concept must appear in exactly one section.\n"
    f"- No section may hold more than {MAX_SECTION_CONCEPTS} concepts. A group "
    "heading past that covers two units, so split it at the most natural seam "
    "and title each half for what it teaches.\n"
    "- ORDER BEFORE lists teaching order: if 'A before B' appears, the section "
    "holding A must come before the section holding B, or both must be in the "
    "same section. Respect every one of them.\n"
    "- GROUP TOGETHER lists pairs a learner is likely to confuse. Put such a "
    "pair in the same section wherever the ordering rules allow, so the note "
    "can draw the distinction.\n"
    "- A section title is a short noun phrase naming what the section teaches, "
    "at most 8 words. Do not number the titles.\n"
    "- Order the sections so the note reads from foundations to consequences.\n"
    'Respond ONLY as JSON: {"sections": [{"title": "...", "concepts": ["...", "..."]}]}.'
)


def build_plan_prompt(concept_names, ordering, cohesion, degree) -> str:
    """
    The user half of the plan prompt.

    Trimmed to the caps at the top of this module. When there is more material
    than fits, the best-connected concepts and the constraints between them are
    kept: a constraint between two hub concepts shapes the plan far more than
    one between two leaves.
    """
    ranked = sorted(concept_names, key=lambda n: (-degree.get(n, 0), n))
    kept = set(ranked[:_MAX_PROMPT_CONCEPTS])

    def weight(pair):
        return -(degree.get(pair[0], 0) + degree.get(pair[1], 0))

    ordering = [p for p in ordering if p[0] in kept and p[1] in kept]
    cohesion = [p for p in cohesion if p[0] in kept and p[1] in kept]
    ordering = sorted(ordering, key=weight)[:_MAX_PROMPT_ORDERING]
    cohesion = sorted(cohesion, key=weight)[:_MAX_PROMPT_COHESION]

    least, most = section_range(len(kept))
    lines = [
        f"{len(kept)} concepts to place. Produce between {least} and {most} sections."
        + (f" Fewer than {least} forces a section past the {MAX_SECTION_CONCEPTS}-concept"
           f" limit; more than {most} leaves sections of under"
           f" {MIN_SECTION_CONCEPTS} concepts." if least != most else
           f" That is the only count that keeps every section between"
           f" {MIN_SECTION_CONCEPTS} and {MAX_SECTION_CONCEPTS} concepts."),
        "",
        "CONCEPTS:",
        ", ".join(sorted(kept)),
    ]
    if ordering:
        lines += ["", "ORDER BEFORE:"]
        lines += [f"- {a} before {b}" for a, b in ordering]
    if cohesion:
        lines += ["", "GROUP TOGETHER:"]
        lines += [f"- {a} + {b}" for a, b in cohesion]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# response validation
# ---------------------------------------------------------------------------

class _PlannedSection(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    concepts: list[str] = Field(min_length=1)


class _PlanResponse(BaseModel):
    sections: list[_PlannedSection] = Field(min_length=1)


# ---------------------------------------------------------------------------
# plan cache
#
# Same shape as graph_fusion's verdict cache, and for the same reason: prompt
# iteration should not re-bill an identical call. Keyed on the prompt version
# and the model, so changing either ignores stale entries rather than reusing
# them. This is a development convenience, not persistence — notes themselves
# live in _NOTES and die with the process.
#
# Written under backend/data/outputs (app.config.OUTPUT_DIR), alongside the
# extraction results and the fusion verdict cache, so every generated artefact
# lives in the one gitignored tree.
# ---------------------------------------------------------------------------

PLAN_CACHE_FILE = OUTPUT_DIR / "notes_plan_cache.json"
WRITE_CACHE_FILE = OUTPUT_DIR / "notes_write_cache.json"


def _load_cache(path: Path) -> dict:
    """Read an on-disk cache; empty dict if absent or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    """Write a cache; best-effort, a failure is not fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# Read through the module globals rather than binding the paths, so a test can
# point either cache somewhere else.
def _load_plan_cache() -> dict:
    return _load_cache(PLAN_CACHE_FILE)


def _save_plan_cache(cache: dict) -> None:
    _save_cache(PLAN_CACHE_FILE, cache)


def _load_write_cache() -> dict:
    return _load_cache(WRITE_CACHE_FILE)


def _save_write_cache(cache: dict) -> None:
    _save_cache(WRITE_CACHE_FILE, cache)


def _plan_key(version: str, document_ids: list[int], days: Optional[int]) -> str:
    """
    Cache key for one plan. Day count is part of it even though splitting is
    deterministic and needs no LLM call, so an entry always corresponds to
    exactly one returned plan rather than to a stage of one.
    """
    ids = ",".join(str(i) for i in sorted(document_ids))
    return f"{version}|{evolution_data.LLM_MODEL}|{ids}|days={days or 0}"


def _call_key(prefix: str, kind: str, concept_names) -> str:
    """
    Cache key for one call inside a plan.

    Planning makes several calls now, and the plan-level key alone no longer
    identifies any one of them. What does identify a call is the set of
    concepts it was asked about, so that set is hashed onto the end. Sorted
    first, so the same subproblem reached by a different route hits the same
    entry.
    """
    digest = hashlib.sha1(
        "\x00".join(sorted(concept_names)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}|{kind}:{digest}"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

_client = None


def _low_reasoning(client):
    """
    Wrap a Groq client so every call pins reasoning_effort="low".

    gpt-oss-120b is a reasoning model, and left at its default it spends the
    entire max_tokens budget deliberating and returns an *empty* completion,
    which Groq then rejects in json_object mode as invalid JSON. Measured on
    the 35-concept network-flow graph: default effort gave finish_reason
    "length", 4000 completion tokens and zero content; "low" gave valid JSON
    in 1581. Grouping concepts is data, not deliberation — quiz_service pins
    it for the same reason.

    evolution_data._call_with_retry builds its own kwargs and has no
    passthrough, so the setting is injected here rather than by changing that
    module. Only .chat.completions.create is used, so a stand-in of that one
    path is enough.
    """
    create = client.chat.completions.create

    def create_low(**kwargs):
        kwargs.setdefault("reasoning_effort", "low")
        return create(**kwargs)

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_low))
    )


def _llm_client():
    """
    Lazily built, so importing this module needs no key. The call itself goes
    through evolution_data._call_with_retry, which owns the retry, the
    Retry-After handling, the token-per-minute pacer and the concurrency
    limit — this is only the client object that helper needs handed to it.
    """
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            return None
        try:
            from groq import Groq
        except ImportError:
            return None
        # max_retries=1 leaves retrying to _call_with_retry rather than doubling
        # it up at the SDK layer.
        _client = _low_reasoning(Groq(api_key=key, max_retries=1))
    return _client


async def _request_groups(system: str, user: str, cache_key: str, what: str):
    """
    One call that asks the model to put concepts into titled groups, cached.

    Dividing and grouping differ only in their prompt — both return
    {"title", "concepts"} objects and both validate against _PlanResponse — so
    the retry, timeout, validation and caching live here once. Returns the list
    of groups, or None when the call or its validation failed.
    """
    cache = _load_plan_cache()
    cached = cache.get(cache_key)
    if isinstance(cached, list) and cached:
        return cached

    client = _llm_client()
    if client is None:
        return None

    try:
        raw = await asyncio.wait_for(
            evolution_data._call_with_retry(
                client, system, user,
                json_mode=True,
                temperature=0.2,
                # Headroom over the largest measured grouping call: the
                # 35-concept network-flow graph used 1581 completion tokens
                # at the pinned low reasoning effort (see _low_reasoning).
                max_tokens=3000,
            ),
            timeout=_PLAN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"Notes plan: {what} call exceeded {_PLAN_TIMEOUT_SECONDS}s — giving up.")
        return None
    if not raw:
        return None

    try:
        parsed = _PlanResponse.model_validate_json(raw)
    except ValidationError as exc:
        print(f"Notes plan: model returned an unusable {what} — {exc}")
        return None

    groups = [{"title": s.title.strip(), "concepts": s.concepts} for s in parsed.sections]
    cache[cache_key] = groups
    _save_plan_cache(cache)
    return groups


async def request_sections(concept_names, ordering, cohesion, degree, cache_key):
    """Asks the model to group a set that already fits one call into sections."""
    return await _request_groups(
        _PLAN_SYSTEM,
        build_plan_prompt(concept_names, ordering, cohesion, degree),
        cache_key, "grouping",
    )


async def request_divide(concept_names, parts_wanted, cache_key):
    """Asks the model for a coarse first cut of a set too large to group."""
    return await _request_groups(
        _DIVIDE_SYSTEM,
        build_divide_prompt(concept_names, parts_wanted),
        cache_key, "division",
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def reconcile_sections(sections, concept_names, edges):
    """
    Maps the model's answer back onto the real concept set.

    The model is told to copy names exactly and mostly does, but it changes
    case, swaps in a typographic hyphen and occasionally invents. Names are
    matched on their normalised form; unknown ones are dropped; names it placed
    twice stay in the first section; anything it missed is placed into whichever
    section already holds most of its neighbours, so a stray concept lands
    beside its own material rather than in a bin at the end.
    """
    by_key = {_name_key(n): n for n in concept_names}
    placed, out = set(), []

    for s in sections:
        names = []
        for raw in s["concepts"]:
            real = by_key.get(_name_key(raw))
            if real and real not in placed:
                placed.add(real)
                names.append(real)
        if names:
            out.append({"title": s["title"], "concepts": names})

    if not out:
        return []

    missing = [n for n in concept_names if n not in placed]
    if missing:
        neighbours: dict[str, set] = defaultdict(set)
        for e in edges:
            neighbours[e["from"]].add(e["to"])
            neighbours[e["to"]].add(e["from"])
        for name in missing:
            near = neighbours.get(name, set())
            best = max(
                range(len(out)),
                key=lambda i: len(near.intersection(out[i]["concepts"])),
            )
            out[best]["concepts"].append(name)

    return out


def _edges_within(edges: list[dict], names) -> list[dict]:
    """Edges with both endpoints inside a subset — the constraints that still
    apply once planning descends into one part."""
    inside = set(names)
    return [e for e in edges if e["from"] in inside and e["to"] in inside]


async def plan_sections(concept_names, edges, key_prefix, trace, depth: int = 0):
    """
    Groups a concept set into sections, dividing first when it is too large to
    put in one prompt.

    A set within _MAX_PROMPT_CONCEPTS is grouped directly. Anything larger is
    cut into parts that do fit and each part is planned by this same function,
    so depth follows from the concept count rather than from a rule per size.
    Sections come back flat and in order: the parts are scaffolding for the
    planning, not a level in the note.

    Two things stop it running away, both because the model may ignore the
    sizes it was asked for. Depth is capped, and a division that returns a part
    no smaller than what went in is discarded — in either case what is on hand
    is grouped instead of being divided again. Both outcomes are recorded in
    `trace`, since grouping an oversized set is exactly the case where concepts
    beyond the prompt cap get placed by the neighbour fallback rather than by
    the model.

    Returns the sections, or None if a call failed.
    """
    n = len(concept_names)

    async def group_here(reason):
        ordering, cohesion = derive_constraints(edges)
        planned = await request_sections(
            concept_names, ordering, cohesion, _degree(edges),
            _call_key(key_prefix, "group", concept_names),
        )
        if planned is None:
            return None
        sections = reconcile_sections(planned, concept_names, edges)
        trace.append({"depth": depth, "kind": "group", "reason": reason,
                      "concepts": n, "produced": len(sections)})
        return sections

    if n <= _MAX_PROMPT_CONCEPTS:
        return await group_here("fits one call")
    if depth >= _MAX_PLAN_DEPTH:
        return await group_here("depth cap reached")

    wanted = min(_MAX_DIVIDE_PARTS, max(2, -(-n // _MAX_PROMPT_CONCEPTS)))
    divided = await request_divide(
        concept_names, wanted, _call_key(key_prefix, "divide", concept_names),
    )
    parts = reconcile_sections(divided, concept_names, edges) if divided else []

    if len(parts) < 2 or any(len(p["concepts"]) >= n for p in parts):
        biggest = max((len(p["concepts"]) for p in parts), default=0)
        print(f"Notes plan: division of {n} concepts came back as {len(parts)} part(s), "
              f"largest {biggest} — not smaller, so grouping without dividing.")
        trace.append({"depth": depth, "kind": "divide", "reason": "rejected",
                      "concepts": n, "produced": len(parts)})
        return await group_here("division did not shrink the set")

    trace.append({"depth": depth, "kind": "divide", "reason": f"{wanted} parts asked",
                  "concepts": n, "produced": len(parts)})

    out = []
    for part in parts:
        names = part["concepts"]
        sub = await plan_sections(
            names, _edges_within(edges, names), key_prefix, trace, depth + 1,
        )
        if sub is None:
            return None
        out += sub
    return out


def summarise_trace(trace: list[dict]) -> dict:
    """Depth reached and what happened at each level, for the response."""
    levels = {}
    for step in trace:
        row = levels.setdefault(step["depth"], {
            "depth": step["depth"], "divide_calls": 0, "group_calls": 0,
            "concepts_seen": 0, "produced": 0, "reasons": [],
        })
        row["divide_calls" if step["kind"] == "divide" else "group_calls"] += 1
        row["concepts_seen"] += step["concepts"]
        row["produced"] += step["produced"]
        if step["reason"] not in row["reasons"]:
            row["reasons"].append(step["reason"])
    return {
        "depth": max(levels) if levels else 0,
        "calls": len(trace),
        "levels": [levels[d] for d in sorted(levels)],
    }


def split_across_days(sizes: list[int], days: Optional[int]) -> list[Optional[int]]:
    """
    Day number per section, or a list of None when no split was asked for.

    Balances on concepts rather than on sections. Dividing four sections over
    three days by position alone gave 21 / 5 / 9 concepts on the network-flow
    graph, because a 14-concept section counted the same as a 5-concept one.
    This walks the sections in order, keeping them intact, and closes the
    current day once taking the next section would carry it more than halfway
    past an even share.

    A section larger than the target still overshoots on its own — it cannot be
    divided here without breaking the grouping the model chose — so a day
    holding one is simply heavier than the rest. Days are only advanced while
    enough sections remain to give every later day at least one.
    No LLM call — this is arithmetic.
    """
    count = len(sizes)
    if not days or count == 0:
        return [None] * count

    days = max(1, min(days, count))
    target = sum(sizes) / days

    out, day, acc = [], 1, 0
    for i, size in enumerate(sizes):
        if day < days:
            # Staying would leave a later day with no section at all.
            starves_a_later_day = (count - i - 1) < (days - day)
            # Otherwise move on once this day has taken its share, counting a
            # section as belonging to the day its bulk falls in.
            day_is_full = acc and acc + size / 2 > target
            if starves_a_later_day or day_is_full:
                day += 1
                acc = 0
        out.append(day)
        acc += size
    return out


def build_note(user_id, title, document_ids, filenames, sections,
               sources_by_concept, edges, days):
    """Assembles the note and its sections. Bodies stay empty — that is the
    write step's job."""
    note_id = _next_id("note")
    day_of = split_across_days([len(s["concepts"]) for s in sections], days)

    rows = []
    for index, section in enumerate(sections):
        names = section["concepts"]
        inside = set(names)
        rows.append({
            "id": _next_id("section"),
            "note_id": note_id,
            "index": index,
            "day": day_of[index],
            "title": section["title"],
            # Recorded now so the write step does not have to re-derive any of
            # it — by then the engine may hold a different graph, or none.
            "concepts": names,
            "relations": [
                e for e in edges if e["from"] in inside and e["to"] in inside
            ],
            "sources": sorted(
                {s for n in names for s in sources_by_concept.get(n, ())}
            ),
            "body": None,
            "done": False,
        })

    return {
        "id": note_id,
        "user_id": user_id,
        "title": title,
        "kind": "plan" if days else "full",
        "days": max((d for d in day_of if d), default=None),
        "document_ids": document_ids,
        "documents": sorted(filenames),
        "created_at": time.time(),
        "sections": rows,
    }


def _resolve_documents(user_id: int, document_ids: list[int]) -> dict[int, str]:
    """
    Document ids to filenames, scoped to the signed-in user.

    The engine keys concepts by `source`, which is the filename passed to
    /api/upload, not the row id — so ids have to be resolved before the graph
    can be filtered at all.
    """
    unique = list(dict.fromkeys(document_ids))
    placeholders = ",".join("?" for _ in unique)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, filename FROM documents WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *unique),
        ).fetchall()

    found = {r["id"]: r["filename"] for r in rows}
    missing = [i for i in unique if i not in found]
    if missing:
        listed = ", ".join(str(i) for i in missing)
        raise HTTPException(
            status_code=404,
            detail=f"Not in your library: document {listed}.",
        )
    return found


def _title_for(filenames: list[str]) -> str:
    if len(filenames) == 1:
        return Path(filenames[0]).stem
    return "Revision notes"


# ---------------------------------------------------------------------------
# write step
#
# Turns one planned section into prose. Runs when the reader opens the section,
# and the result is stored on it, so a note costs one write per section a
# reader actually reaches rather than one per section at plan time.
# ---------------------------------------------------------------------------

# v2: the first version produced a summary rather than notes. It narrated the
# supplied relation list back as prose — "differential equations enable
# Newton's second law" — using the raw labels as verbs, and opened the lead
# with "This section examines...". So the labels are now banned from the
# output, the lead must be an assertion, and the prompt leads with the extracts
# instead of the relations.
#
# v3: terms came back as whole sentences lifted from the extracts — "Strict
# Determinism: In the classical model, if one were to know the exact position
# and momentum of every particle..." — which is an explanation, not a glossary
# entry. The rule now asks for one short sentence naming what the thing is,
# and says to drop a concept that cannot be defined that briefly.
#
# v4: the bodies read as flat prose with nothing for the eye to stop on. A
# paragraph is now a heading plus marked spans rather than a plain string, so
# the first mention of a concept and any figure that matters come back bold,
# one sentence per paragraph is marked as the claim worth keeping, and the
# headings let a section be scanned instead of read start to finish.
#
# v5: three display faults. Headings came back in Title Case and the key
# highlight started part-way through its sentence, because v4 forbade a span
# from being both bold and key — so a sentence containing a first mention got
# split and only the tail was marked. Both rules are relaxed here. Text is also
# NFKC-normalised now, which folds the OHM SIGN the model used for Ω and the
# ligatures the PDFs carry.
#
# Each bump changes the cache key, so bodies written under an older prompt are
# never served.
_WRITE_PROMPT_VERSION = "notes-write-v5"

# Pinned, not the "-latest" alias document_service uses for OCR. That alias
# was returning 503 UNAVAILABLE on every attempt while this exact model
# answered in about a second, and a moving alias makes a cached body
# irreproducible anyway. Groq is pinned the same way, to openai/gpt-oss-120b.
GEMINI_MODEL = "gemini-2.5-flash"

_WRITE_SYSTEM = (
    "You write one section of a student's revision notes. You are writing for "
    "someone revising for an exam, not summarising a document for a reader who "
    "will never see it.\n"
    "\n"
    "THE SOURCE EXTRACTS ARE WHAT YOU WRITE FROM. Every explanation, example "
    "and definition comes out of them. Invent nothing they do not support.\n"
    "\n"
    "THE RELATIONS ARE NOT MATERIAL TO WRITE FROM. They were extracted from "
    "the same documents and tell you only which connections matter enough to "
    "cover, so you can check you have not left one out. They are a checklist, "
    "not a source.\n"
    "- NEVER write a relation label as a word. Words like 'enables', "
    "'explains', 'part of', 'is a type of', 'related to', 'prerequisite' and "
    "every other label in the list are forbidden in your output. Saying "
    "'differential equations enable Newton's second law' restates the "
    "checklist; saying what the dependency actually consists of is the note.\n"
    "- Explain what a connection MEANS — what it lets you do, what breaks "
    "without it, why one idea has to come before another — using the extracts "
    "to say it.\n"
    "- Never state a connection that is not in the list, and never reverse "
    "one. If no relations are listed, the concepts have no recorded links to "
    "each other; write the section without them and do not mention it.\n"
    "\n"
    "Fill these blocks:\n"
    "- lead: ONE sentence, written as an assertion, giving the single thing a "
    "reader must hold onto after this section. A claim, not a description of "
    "what follows. It must NOT begin with 'This section', 'This part', 'Here "
    "we', 'In this' or any other announcement, and must not restate the "
    "title.\n"
    "- body: two to four paragraphs of explanation, in the order a reader "
    "should meet the ideas. Each paragraph is a heading plus a list of spans, "
    "and the spans joined in order are the paragraph's prose — so put the "
    "spaces in, and do not repeat text between spans.\n"
    "  * heading: three to six words naming what THIS paragraph covers, so the "
    "section can be scanned without reading it. Not a title for the whole "
    "section, and not a sentence. Sentence case — capitalise the first word "
    "and any proper noun, nothing else, and no full stop. Write 'Why height "
    "matters', not 'Why Height Matters'.\n"
    "  * bold: true on the FIRST mention of each concept the section covers, "
    "and on a figure that carries weight — an O(h), a Θ(n lg n), a ±1 balance "
    "condition. First mention only; later ones are plain.\n"
    "  * key: true on the ONE sentence in the paragraph a reader would keep if "
    "they kept nothing else. Exactly one sentence per paragraph, and mark it "
    "from its first word to its full stop — not from part-way through.\n"
    "  * A span may be both bold and key. When the sentence worth keeping "
    "contains the first mention of a concept, split it into spans that are ALL "
    "key, and set bold as well on the one holding the concept. Do not start "
    "the key run after the bold word; that highlights half a sentence.\n"
    "  * Everything else is plain: bold false, key false.\n"
    "  * Keep it sparse. Outside the key sentence, no more than a few words a "
    "paragraph should be bold. If everything is marked, nothing is.\n"
    "- example: a worked example, ONLY if the extracts contain one to work "
    "from. Otherwise null.\n"
    "- table: a comparison table, ONLY if the section genuinely contrasts two "
    "or more things along shared dimensions. Otherwise null.\n"
    "- terms: a glossary, ONLY for terms the extracts actually define. Each "
    "entry is ONE short sentence naming what the thing IS — a definition, not "
    "an explanation of why it matters or how it behaves. 'Tree height: edges "
    "on the longest root-to-leaf path' is the shape; a sentence beginning 'If "
    "one were to know...' or 'In the classical model...' is not. Do not lift a "
    "sentence out of the extracts to serve as one. If a concept cannot be "
    "defined that briefly, leave it out — the body covers it. Otherwise null.\n"
    "\n"
    "A block with no material is null. Never pad one to fill it, and never "
    "produce an empty list or an example invented to have something there."
)


class _WriteTerm(BaseModel):
    term: str
    definition: str


class _WriteExample(BaseModel):
    title: str
    paragraphs: list[str]


class _WriteTable(BaseModel):
    columns: list[str]
    rows: list[list[str]]


class _WriteSpan(BaseModel):
    """
    A run of paragraph text and how it is marked.

    `bold` and `key` are required rather than defaulted, so the model has to
    decide for every span instead of quietly omitting them — soft instructions
    about emphasis were what produced flat prose in the first place. The shape
    matches what the frontend's Rich renderer already reads.
    """
    text: str
    bold: bool
    key: bool


class _WriteParagraph(BaseModel):
    heading: str
    parts: list[_WriteSpan]


class _WriteBody(BaseModel):
    """The fixed block structure. Gemini takes this as a responseSchema, so the
    shape is enforced by the API rather than asked for in the prompt and
    validated afterwards."""
    lead: str
    body: list[_WriteParagraph]
    example: Optional[_WriteExample] = None
    table: Optional[_WriteTable] = None
    terms: Optional[list[_WriteTerm]] = None


_gemini_client_instance = None


def _gemini_client():
    """
    Built on first use, not at import — the same reason document_service
    defers its client: constructing one without a key raises, and doing that
    at import time took the whole server down before it could serve anything.
    Returns None when no key is set, and the route turns that into a 503.

    GEMINI_API_KEY is preferred; GEMINI_VISION_API_KEY is accepted so an
    install already set up for image OCR needs no second key.
    """
    global _gemini_client_instance
    if _gemini_client_instance is None:
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_VISION_API_KEY")
        if not key:
            return None
        try:
            from google import genai
        except ImportError:
            return None
        _gemini_client_instance = genai.Client(api_key=key)
    return _gemini_client_instance


def _write_key(section: dict) -> str:
    """
    Cache key for one section's prose: prompt version, model, and what the
    section is. Identity is the title, concepts and relations recorded at plan
    time — regenerate the same plan and the same section hits the same entry.

    The retrieved extracts are deliberately not part of it, matching the plan
    cache, which keys on its inputs and not on the evidence behind them. A
    corpus that has changed under a stored note is a staleness problem for the
    note itself, not something a cache key can fix.
    """
    identity = json.dumps({
        "title": section["title"],
        "concepts": sorted(section["concepts"]),
        "relations": sorted(
            [e.get("from", ""), e.get("relation", ""), e.get("to", "")]
            for e in section["relations"]
        ),
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"{_WRITE_PROMPT_VERSION}|{GEMINI_MODEL}|section:{digest}"


async def gather_chunks(concept_names) -> list[dict]:
    """
    Source text behind a section, one retrieval per concept name.

    retrieve_chunks embeds the query and hits the vector store, so it blocks;
    all of the calls for one section share a single thread hop. Chunks are
    deduplicated on their text, because neighbouring concepts routinely
    retrieve the same passage, and kept whole — the write step is the one place
    that wants the full chunk rather than a snippet.
    """
    engine = _engine()
    retrieve = getattr(engine, "retrieve_chunks", None)
    if retrieve is None:
        return []

    def collect():
        found, seen = [], set()
        for name in concept_names:
            try:
                hits = retrieve(name, top_k=_CHUNKS_PER_CONCEPT)
            except Exception as exc:            # a broken store is not a 500
                print(f"Notes write: chunk retrieval failed for {name!r} — {exc}")
                continue
            for hit in hits or []:
                # Normalised on the way in as well as out, so the model never
                # sees the ligatures the PDFs carry and cannot copy them back.
                text = normalise_unicode((hit.get("text") or "").strip())
                if text and text not in seen:
                    seen.add(text)
                    found.append({**hit, "text": text})
        found.sort(key=lambda h: h.get("similarity") or 0, reverse=True)
        return found[:_MAX_WRITE_CHUNKS]

    return await asyncio.to_thread(collect)


def build_write_prompt(section: dict, chunks: list[dict]) -> str:
    """
    The material half of the write prompt.

    Extracts come first and are labelled as the thing being written from;
    relations follow as a coverage checklist. The first version led with the
    relations, and the model treated whatever came first as its material —
    it wrote the edge list back as prose and barely touched the source text.
    Order carries that weight, so the sections are ordered deliberately.
    """
    lines = [f"SECTION TITLE: {section['title']}", "",
             "CONCEPTS THIS SECTION COVERS:", ", ".join(section["concepts"]), ""]

    lines.append("SOURCE EXTRACTS — write the section from these:")
    if chunks:
        for i, chunk in enumerate(chunks, 1):
            where = chunk.get("source") or "unknown document"
            lines += [f"[{i}] from {where}", chunk["text"], ""]
    else:
        lines += ["(none retrieved)", ""]

    if section["relations"]:
        lines += [
            "CONNECTIONS TO COVER — a checklist, not material to quote. Each "
            "line is a link worth explaining; explain what it means in the "
            "words of the extracts above, and never use the label itself:",
        ]
        lines += [f"- {e['from']} / {e.get('relation', 'relates to')} / {e['to']}"
                  for e in section["relations"]]
    else:
        lines.append("CONNECTIONS TO COVER: none recorded between these concepts.")
    return "\n".join(lines)


# Labels too ordinary to police. Banning "has" or "is a" from English prose
# would fire on almost every section and the rewrite would be worse than the
# sentence it replaced, so a relation carrying one of these is not checked.
_LABEL_STOPWORDS = frozenset({"is", "are", "was", "were", "be", "been",
                              "has", "have", "had", "is a", "is an"})

_SENTENCE = re.compile(r"[^.!?]+[.!?]?")


def _label_pattern(label: str) -> Optional[re.Pattern]:
    """
    A regex that catches one relation label used as a word.

    A phrase is matched literally. A single word is reduced to its stem and
    matched with the inflections a writer would reach for, so "enables" also
    catches enable, enabled and enabling — but the endings are anchored, so
    "causes" does not catch "causality", which is a concept name in its own
    right. Short labels stay literal: the stem of "uses" is "us".
    """
    if not label or label in _LABEL_STOPWORDS:
        return None
    if " " in label or len(label) < 6:
        return re.compile(rf"\b{re.escape(label)}\b")
    stem = re.sub(r"(?:ies|es|ed|ing|s)$", "", label)
    if len(stem) < 4:
        return re.compile(rf"\b{re.escape(label)}\b")
    return re.compile(rf"\b{re.escape(stem)}(?:e|es|s|ed|ing|d)?\b")


def normalise_unicode(value):
    """
    NFKC over every string in a nested structure.

    Two real problems, one fix. The model reaches for compatibility
    codepoints — it wrote Ω as U+2126 OHM SIGN rather than U+03A9 GREEK
    CAPITAL OMEGA, which most fonts have no glyph for, so it rendered as a
    box. And the lecture PDFs themselves carry ligatures, so "Definition"
    arrives out of extraction as "Deﬁnition" and flows through to the
    note. NFKC folds both to the ordinary letters.

    NFKC rather than NFC because only the compatibility mappings fix these;
    NFC would leave both untouched.
    """
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, list):
        return [normalise_unicode(v) for v in value]
    if isinstance(value, dict):
        return {k: normalise_unicode(v) for k, v in value.items()}
    return value


def paragraph_text(paragraph) -> str:
    """
    A body paragraph as flat text.

    Paragraphs are a heading plus marked spans; joining the spans in order is
    the prose. A plain string is still accepted, so a body written before the
    spans existed can still be checked.
    """
    if isinstance(paragraph, str):
        return paragraph
    if not isinstance(paragraph, dict):
        return ""
    return "".join(
        str(span.get("text") or "")
        for span in paragraph.get("parts") or []
        if isinstance(span, dict)
    )


def _prose_of(body: dict) -> list[str]:
    """The written blocks. The table is left out: it is tabular data, and a
    column header naming a relationship is not the prose restating one."""
    out = [body.get("lead") or ""]
    # Headings are scanned as well: a heading naming a relation label is the
    # same restatement the ban exists to stop.
    for paragraph in body.get("body") or []:
        if isinstance(paragraph, dict):
            out.append(paragraph.get("heading") or "")
        out.append(paragraph_text(paragraph))
    example = body.get("example")
    if example:
        out.append(example.get("title") or "")
        out += list(example.get("paragraphs") or [])
    for term in body.get("terms") or []:
        out.append(term.get("definition") or "")
    return [p for p in out if p]


def find_label_violations(body: dict, relations: list[dict]) -> list[tuple[str, str]]:
    """
    Sentences that use one of this section's own relation labels as a word.

    The ban is in the prompt and the model half-keeps it, so it is checked
    here too. Returns (label, sentence) pairs, at most one per label, so the
    rewrite instruction names each problem once.
    """
    patterns = {}
    for edge in relations or []:
        label = _normalise_relation(edge.get("relation"))
        if label in patterns:
            continue
        pattern = _label_pattern(label)
        if pattern is not None:
            patterns[label] = pattern

    found: list[tuple[str, str]] = []
    seen = set()
    for paragraph in _prose_of(body):
        for sentence in _SENTENCE.findall(paragraph):
            text = sentence.strip()
            if not text:
                continue
            for label, pattern in patterns.items():
                if label not in seen and pattern.search(text.lower()):
                    seen.add(label)
                    found.append((label, text))
    return found


def _rewrite_instruction(violations: list[tuple[str, str]]) -> str:
    """The correction appended to the prompt for the one retry."""
    lines = [
        "",
        "",
        "YOUR PREVIOUS ANSWER BROKE THE RULE ABOUT CONNECTION LABELS.",
        "Each sentence below states a connection by naming its label instead of "
        "saying what the connection means. Rewrite these sentences so the "
        "labelled word does not appear at all — say what the dependency "
        "consists of, in the words of the extracts. Leave everything else as "
        "it was, and return the whole answer again.",
        "",
    ]
    for label, sentence in violations:
        lines.append(f'- the word "{label}" in: {sentence}')
    return "\n".join(lines)


def _is_transient(exc: Exception) -> bool:
    """
    True for a failure worth retrying: the model is busy or the quota window
    is full, not the request being wrong.

    Matched on the status code where the SDK exposes one and on the text
    otherwise, so this does not have to import google.genai.errors at module
    level — the whole point of the lazy client is that this module imports
    without the Gemini stack present.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc).upper()
    return any(s in text for s in
               ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED",
                "INTERNAL", "429", "503"))


async def write_body(section: dict) -> Optional[dict]:
    """
    Writes one section's prose. Returns the block structure, or None when the
    call failed. Consults the write cache first and writes back on success.
    """
    cache_key = _write_key(section)
    cache = _load_write_cache()
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("lead"):
        return cached

    client = _gemini_client()
    if client is None:
        return None

    chunks = await gather_chunks(section["concepts"])
    if not chunks:
        print(f"Notes write: no source text retrieved for section {section['id']} "
              f"({len(section['concepts'])} concepts) — nothing to write from.")
        return None

    prompt = build_write_prompt(section, chunks)
    body = await _generate_body(client, prompt)
    if body is None:
        return None

    # The label ban is a prompt rule the model only half-keeps, so it is
    # enforced here: one rewrite, with the offending sentences quoted back.
    violations = find_label_violations(body, section["relations"])
    if violations:
        print(f"Notes write: section {section['id']} used "
              f"{len(violations)} relation label(s) as words "
              f"({', '.join(l for l, _ in violations)}) — asking for a rewrite.")
        second = await _generate_body(client, prompt + _rewrite_instruction(violations))
        if second is not None:
            # Taken either way. A second pass that still slips is better than
            # a loop, and the count below records what got through.
            body = second
            left = find_label_violations(body, section["relations"])
            print(f"Notes write: rewrite {'clean' if not left else 'still using ' + ', '.join(l for l, _ in left)}.")
        else:
            print("Notes write: rewrite call failed — keeping the first answer.")

    body["label_violations"] = [
        label for label, _ in find_label_violations(body, section["relations"])
    ]
    body["sources"] = sorted({c.get("source") for c in chunks if c.get("source")})
    body["chunks_used"] = len(chunks)

    cache[cache_key] = body
    _save_write_cache(cache)
    return body


async def _generate_body(client, prompt: str) -> Optional[dict]:
    """
    One Gemini call for a section body, retried on transient failures and
    parsed against the schema. Returns the block dict, or None.
    """
    from google.genai import types

    def call():
        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_WRITE_SYSTEM,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=_WriteBody,
                # No tools are passed, so the SDK's automatic function calling
                # has nothing to do; left on it warns on every single call.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )

    response = None
    for attempt in range(_WRITE_MAX_ATTEMPTS):
        last = attempt == _WRITE_MAX_ATTEMPTS - 1
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(call), timeout=_WRITE_TIMEOUT_SECONDS
            )
            break
        except asyncio.TimeoutError:
            print(f"Notes write: call exceeded {_WRITE_TIMEOUT_SECONDS}s — giving up.")
            return None
        except Exception as exc:
            if last or not _is_transient(exc):
                print(f"Notes write: Gemini call failed — {type(exc).__name__}: {exc}")
                return None
            delay = _WRITE_RETRY_BASE_SECONDS * (3 ** attempt)
            print(f"Notes write: {type(exc).__name__} on attempt "
                  f"{attempt + 1}/{_WRITE_MAX_ATTEMPTS} — retrying in {delay}s.")
            await asyncio.sleep(delay)
    if response is None:
        return None

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        # The schema is enforced server-side, so this is unusual; parse the
        # text rather than lose the answer to it.
        try:
            parsed = _WriteBody.model_validate_json(response.text or "")
        except (ValidationError, AttributeError) as exc:
            print(f"Notes write: unusable response — {exc}")
            return None

    body = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
    return normalise_unicode(body)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

class PlanRequest(BaseModel):
    document_ids: list[int] = Field(min_length=1)
    days: Optional[int] = Field(default=None, ge=1, le=30)


@router.post("/api/notes/plan", status_code=201)
async def plan_note(body: PlanRequest, user=Depends(current_user)):
    """
    Groups the concepts of the given documents into revision sections.

    Sections come back empty: this decides what each one covers, not what it
    says. Filling a body in is the write step.
    """
    documents = _resolve_documents(user["id"], body.document_ids)
    filenames = set(documents.values())

    names_by_source, sources_by_concept, edges = filter_graph_to_documents(filenames)
    concept_names = sorted(sources_by_concept)

    if not concept_names:
        raise HTTPException(
            status_code=400,
            detail="Nothing has been extracted from those documents yet. "
                   "Wait for processing to finish, then try again.",
        )
    # Derived here only for the diagnostics below. Planning re-derives them per
    # subproblem, since a part's constraints are the edges inside that part.
    ordering, cohesion = derive_constraints(edges)

    # A corpus whose extractor favours verbs this vocabulary does not know
    # still plans, it just plans with no sense of order — and looks identical
    # to a corpus whose relations are genuinely all symmetric. Say so.
    unrecognised = unrecognised_labels(edges)
    if not ordering:
        print(f"Notes plan: no ordering constraints from {len(edges)} edges — "
              f"sections will not be sequenced. Unrecognised labels: "
              f"{list(unrecognised) or 'none'}")
    elif unrecognised:
        print(f"Notes plan: {sum(unrecognised.values())} of {len(edges)} edges carry a "
              f"label the classifier does not know: {list(unrecognised)[:8]}")

    trace: list[dict] = []
    sections = await plan_sections(
        concept_names, edges,
        _plan_key(_PLAN_PROMPT_VERSION, body.document_ids, body.days),
        trace,
    )
    if sections is None:
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later.",
        )

    if not sections:
        raise HTTPException(
            status_code=503,
            detail="The AI Service returned an unusable plan. Please try again.",
        )

    note = build_note(
        user_id=user["id"],
        title=_title_for(sorted(filenames)),
        document_ids=list(dict.fromkeys(body.document_ids)),
        filenames=filenames,
        sections=sections,
        sources_by_concept=sources_by_concept,
        edges=edges,
        days=body.days,
    )
    _save_note(user["id"], note)

    # Reported alongside the note rather than stored on it: this describes the
    # run that produced the plan, not the plan. `ordering: 0` with a populated
    # `unrecognised` is the signal that sequencing was lost.
    return {
        "note": _note_public(note),
        "constraints": {
            "edges": len(edges),
            "ordering": len(ordering),
            "cohesion": len(cohesion),
            "unrecognised_edges": sum(unrecognised.values()),
            "unrecognised_labels": unrecognised,
        },
        "plan": summarise_trace(trace),
    }


@router.post("/api/notes/sections/{section_id}/write", status_code=201)
async def write_section(section_id: int, user=Depends(current_user)):
    """
    Writes one section's prose, from the concepts and relations the plan step
    recorded on it plus the source text behind those concepts.

    Called when a reader opens the section. The result is stored, so opening it
    again serves what is there instead of writing it twice — `generated` says
    which happened.
    """
    note, section = _load_section(user["id"], section_id)
    if section is None:
        raise HTTPException(status_code=404, detail="That section no longer exists.")

    if section["body"] is not None:
        return {"section": _section_public(section), "note_id": note["id"],
                "generated": False}

    if _gemini_client() is None:
        raise HTTPException(
            status_code=503,
            detail="Note writing is not configured. Set GEMINI_API_KEY in backend/.env.",
        )

    body = await write_body(section)
    if body is None:
        raise HTTPException(
            status_code=503,
            detail="Could not write this section just now. Please try again.",
        )

    section["body"] = body
    return {"section": _section_public(section), "note_id": note["id"],
            "generated": True}


@router.get("/api/notes")
async def list_notes(user=Depends(current_user)):
    """Notes planned in this process, newest first."""
    return {"notes": [_note_public(n) for n in _load_notes(user["id"])]}


@router.get("/api/notes/{note_id}")
async def get_note(note_id: int, user=Depends(current_user)):
    note = _load_note(user["id"], note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="That note no longer exists.")
    return {"note": _note_public(note)}
