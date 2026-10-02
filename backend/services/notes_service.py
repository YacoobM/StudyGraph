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
import logging
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
from services import evolution_data, key_sentences

router = APIRouter(tags=["notes"])
logger = logging.getLogger(__name__)

# Sections the LLM is asked for. Above 7 the sections get too thin to write a
# body from. Left to the bare range the model picks the minimum, which on the
# network-flow graph produced a single 14-concept section covering two
# distinct units, so build_plan_prompt computes a range per call rather than
# leaving it to choose the floor.
#
# MIN_SECTIONS is a floor for large sets only, from SECTION_FLOOR_FROM
# concepts. Applied to every set it forced a 9-concept, 7-page lecture into
# four sections, each written from the same text, so they repeated each other.
MIN_SECTIONS = 4
MAX_SECTIONS = 7
SECTION_FLOOR_FROM = 16

# Source text behind one section, in characters. The section count is capped
# at the documents' text length divided by this, so a short lecture is not
# split more finely than it has material for: Lecture 5 (about 5,400
# characters over 7 pages) gets two sections at most.
SOURCE_CHARS_PER_SECTION = 2500

# A section is what one sitting covers and what the write step turns into one
# body. Past roughly this many concepts it stops being a unit and becomes a
# list. MAX_SECTIONS * MAX_SECTION_CONCEPTS must stay above
# _MAX_PROMPT_CONCEPTS below, or the two instructions cannot both be satisfied.
MAX_SECTION_CONCEPTS = 8

# The other end of the same idea: a section of one concept is a bullet, not a
# study unit. Two per section is the floor, so the requested section count
# scales down with the concept count — four concepts give two sections, not
# four sections of one, and not an error.
MIN_SECTION_CONCEPTS = 2

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
# retrieve_chunks searches every document in the store and has no source
# filter, so each concept asks for this many hits and keeps the first
# _CHUNKS_PER_CONCEPT from the note's own documents. Chroma returns what it
# has when the store is smaller.
_RETRIEVE_POOL = 40
_MAX_WRITE_CHUNKS = 30         # bound on a degraded section holding many concepts
_WRITE_TIMEOUT_SECONDS = 120   # a longer prompt than planning, and a longer answer

# Planning inherits retry and backoff from evolution_data._call_with_retry.
# This call does not go through that helper, so it needs its own: Gemini
# answers a busy model with 503 UNAVAILABLE, and the first version gave up on
# it, turning a transient spike into a failed section. Waits 2s, 6s, 18s.
_WRITE_MAX_ATTEMPTS = 4
_WRITE_RETRY_BASE_SECONDS = 2

# Total time this call will spend waiting between attempts, across all of
# them. A reader is sitting on the HTTP response, so the wait has to stay
# near what the fixed backoff already cost (2 + 6 + 18 = 26s) even when the
# server asks for longer. Reached, or exceeded by the next wait, the call
# gives up instead of holding the request open — and on a per-minute request
# quota that is also the cheaper answer, since every further attempt spends
# the quota it is waiting for.
_WRITE_RETRY_BUDGET_SECONDS = 30

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
        # The summary is the extractor's sentence about the link, and says what
        # it is where the bare label can mislead: "balanced binary search tree
        # causes tree height" was summarised as keeping the height logarithmic.
        # The Connections block shows it in place of the label.
        kept_edges.append({"from": a, "relation": relation, "to": b,
                           "summary": str(e.get("summary") or "").strip()})

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

# v3: the section count is capped by source length and the four-section floor
# applies only to large sets; the size limit moved into the request; titles
# may only name concepts in their own section.
# v4: sections are reordered by where their material first appears in the
# source (order_sections), after the model has grouped them.
# v5: the prompt lists each concept's source position and asks for grouping
# by locality. Lectures 5 + 6 had paired heap (end of Lecture 6) with the
# runway problem (start of Lecture 5).
# v6: positions are measured on the document's content only — no running
# heads, title, author lines, overview list or references — so a concept named
# in the overview is placed where it is taught.
_PLAN_PROMPT_VERSION = "notes-plan-v6"
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


def section_range(n: int, source_cap: Optional[int] = None) -> tuple[int, int]:
    """
    How many sections to ask for, given `n` concepts and at most `source_cap`
    sections' worth of source text (None when unknown).

    Both ends fall out of the per-section size limits rather than being fixed:
    at most MAX_SECTION_CONCEPTS per section sets the floor on the count, at
    least MIN_SECTION_CONCEPTS per section sets the ceiling. MIN_SECTIONS
    raises the floor only from SECTION_FLOOR_FROM concepts.

    The source cap lowers the ceiling and wins over the floor: a short
    document is written as few sections even when that puts more than
    MAX_SECTION_CONCEPTS concepts in one. build_plan_prompt loosens the size
    limit to match.
    """
    most = min(MAX_SECTIONS, max(1, n // MIN_SECTION_CONCEPTS))
    if source_cap is not None:
        most = max(1, min(most, source_cap))
    least = -(-n // MAX_SECTION_CONCEPTS)
    if n >= SECTION_FLOOR_FROM:
        least = max(least, MIN_SECTIONS)
    return min(least, most), most


def section_size_limit(n: int, least: int) -> int:
    """Most concepts one section may hold: MAX_SECTION_CONCEPTS, unless the
    section count was capped so low that `n` cannot fit under it."""
    return max(MAX_SECTION_CONCEPTS, -(-n // max(1, least)))


def document_texts(filenames: set[str]) -> dict[str, str]:
    """
    Each document's extracted text, rebuilt from the engine's chunk store.

    Chunks overlap: chunk i starts at i * (size - overlap), so laying each one
    down at its start rebuilds the text exactly. The ids carry the index as
    "{source}:chunk:{i}:{digest}". Empty when the engine holds no chunks.
    """
    engine = _engine()
    chunks = getattr(engine, "chunks_by_id", None) or {}
    if not chunks:
        return {}
    from services.graph_engine import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
    step = max(DEFAULT_CHUNK_SIZE - DEFAULT_CHUNK_OVERLAP, 1)

    pieces: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for chunk_id, chunk in chunks.items():
        source = chunk.get("source")
        if source not in filenames:
            continue
        try:
            index = int(str(chunk_id).rsplit(":", 2)[1])
        except (IndexError, ValueError):
            index = 0
        pieces[source].append((index, chunk.get("text") or ""))

    texts = {}
    for source, parts in pieces.items():
        text = ""
        for index, part in sorted(parts):
            text = text[:index * step] + part
        texts[source] = text
    return texts


def source_section_cap(filenames: set[str]) -> Optional[int]:
    """How many sections the documents' text can fill, or None when the
    engine holds no chunks for them."""
    total = sum(len(t) for t in document_texts(filenames).values())
    if not total:
        return None
    return max(1, total // SOURCE_CHARS_PER_SECTION)


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
    "- No section may hold more concepts than the request allows. A group "
    "heading past that covers two units, so split it at the most natural seam "
    "and title each half for what it teaches.\n"
    f"- No section may hold fewer than {MIN_SECTION_CONCEPTS} concepts. A lone "
    "concept is a bullet, not a section: there is too little behind it to write "
    "one from. Fold it into the section holding its nearest neighbours instead.\n"
    "- Make the sections roughly even in size. A section far larger than the "
    "others is two units under one heading, and one far smaller has too little "
    "to write a section from.\n"
    "- ORDER BEFORE lists teaching order: if 'A before B' appears, the section "
    "holding A must come before the section holding B, or both must be in the "
    "same section. Respect every one of them.\n"
    "- GROUP TOGETHER lists pairs a learner is likely to confuse. Put such a "
    "pair in the same section wherever the ordering rules allow, so the note "
    "can draw the distinction.\n"
    "- A section title is a short noun phrase naming what the section teaches, "
    "at most 8 words. Do not number the titles.\n"
    "- A title may only name concepts in that section. Do not name a topic, "
    "technique or structure that is not in its concept list, however likely "
    "it seems to belong there.\n"
    "- Order the sections so the note reads from foundations to consequences.\n"
    "- A concept may be tagged with where it first appears in the source: "
    "which document (D1, D2, ...) and how far through it (0% is the start). "
    "Concepts close together in the same document are taught together there, "
    "so group them together. Put concepts from different documents in one "
    "section only when they are strongly related: the same topic, or one "
    "directly built on the other.\n"
    'Respond ONLY as JSON: {"sections": [{"title": "...", "concepts": ["...", "..."]}]}.'
)


def _concept_lines(names, locality) -> list[str]:
    """
    The CONCEPTS block. With positions, one concept per line in source order,
    tagged with where it first appears: "(D2, 40%)" is 40% of the way through
    the second document, and the document tag is left off when there is only
    one. Without them, the old comma-separated list.
    """
    if not locality:
        return ["CONCEPTS:", ", ".join(sorted(names))]
    doc_order, positions = locality
    many = len(doc_order) > 1
    unplaced = (float("inf"), float("inf"))
    lines = []
    if many:
        lines += ["DOCUMENTS:"] + [f"D{i + 1} = {d}" for i, d in enumerate(doc_order)] + [""]
    lines.append("CONCEPTS, in source order, with where each first appears:")
    for name in sorted(names, key=lambda n: (positions.get(n, unplaced), n)):
        spot = positions.get(name)
        if spot is None:
            lines.append(f"- {name} (position unknown)")
        else:
            doc, frac = spot
            where = f"D{doc + 1}, {round(frac * 100)}%" if many else f"{round(frac * 100)}%"
            lines.append(f"- {name} ({where})")
    return lines


def build_plan_prompt(concept_names, ordering, cohesion, degree,
                      source_cap: Optional[int] = None, locality=None) -> str:
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

    least, most = section_range(len(kept), source_cap)
    count = (f"Produce exactly {least} section{'s' if least != 1 else ''}."
             if least == most else
             f"Produce between {least} and {most} sections.")
    lines = [
        f"{len(kept)} concepts to place. {count} No section may hold more than "
        f"{section_size_limit(len(kept), least)} concepts.",
        "",
        *_concept_lines(kept, locality),
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
# Must-cover picks per document, keyed on the picker's version and a hash of
# the document text, so a document is ranked once however many notes use it.
KEYSENT_CACHE_FILE = OUTPUT_DIR / "notes_keysent_cache.json"


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


# Serialises write-cache updates. A section takes 20-30s to write, and several
# can be in flight at once when a reader clicks through a note. Each used to
# save the dict it loaded before generating, so whichever finished last wrote
# back a copy without the others' entries. Lecture 5 lost three of its four
# sections that way.
_WRITE_CACHE_LOCK = asyncio.Lock()


async def _store_written(cache_key: str, body: dict) -> None:
    """Adds one body to the write cache, re-reading the file first so entries
    saved by other sections since this one started are kept."""
    async with _WRITE_CACHE_LOCK:
        cache = _load_write_cache()
        cache[cache_key] = body
        _save_write_cache(cache)


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
        response = create(**kwargs)
        # Actual usage, for re-measuring the max_tokens headroom in
        # _request_groups whenever the model changes.
        usage = getattr(response, "usage", None)
        finish = response.choices[0].finish_reason if response.choices else None
        logger.debug(
            "notes_plan_usage model=%s reasoning_effort=%s completion_tokens=%s "
            "prompt_tokens=%s max_tokens=%s finish_reason=%s",
            kwargs.get("model"), kwargs.get("reasoning_effort"),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "prompt_tokens", None),
            kwargs.get("max_tokens"), finish,
        )
        return response

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
                # That was on the previous model; re-measure from the
                # "Notes plan:" usage line. The shared llm_gateway limiter
                # rejects any single call above LLM_OUTPUT_TOKENS_PER_MINUTE,
                # so this must stay at or below that.
                max_tokens=2000,
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


async def request_sections(concept_names, ordering, cohesion, degree, cache_key,
                           source_cap: Optional[int] = None, locality=None):
    """Asks the model to group a set that already fits one call into sections."""
    return await _request_groups(
        _PLAN_SYSTEM,
        build_plan_prompt(concept_names, ordering, cohesion, degree, source_cap, locality),
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


async def plan_sections(concept_names, edges, key_prefix, trace, depth: int = 0,
                        source_cap: Optional[int] = None, locality=None):
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

    `source_cap` is the most sections the source text can fill (see
    source_section_cap). When the set is divided, each part gets a share of it
    in proportion to its concepts.

    `locality` is (document order, concept positions) from concept_positions,
    shown to the model so it groups what the source teaches together.

    Returns the sections, or None if a call failed.
    """
    n = len(concept_names)

    async def group_here(reason):
        ordering, cohesion = derive_constraints(edges)
        planned = await request_sections(
            concept_names, ordering, cohesion, _degree(edges),
            # The cap changes the prompt, so it is part of what identifies the call.
            _call_key(key_prefix, f"group:cap={source_cap}", concept_names),
            source_cap, locality,
        )
        if planned is None:
            return None
        sections = reconcile_sections(planned, concept_names, edges)
        trace.append({"depth": depth, "kind": "group", "reason": reason,
                      "concepts": n, "produced": len(sections),
                      "source_cap": source_cap})
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
        share = (None if source_cap is None
                 else max(1, round(source_cap * len(names) / n)))
        sub = await plan_sections(
            names, _edges_within(edges, names), key_prefix, trace, depth + 1,
            share, locality,
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


_NATURAL = re.compile(r"(\d+)")


def _natural_key(text: str):
    """Sort key that reads digit runs as numbers: Lecture 5 < Lecture 10."""
    return [int(p) if p.isdigit() else p.lower() for p in _NATURAL.split(text or "")]


def _week_number(week) -> float:
    """'Week 3', '3' or 3 to 3.0; anything else sorts after every numbered week."""
    match = re.search(r"\d+", str(week or ""))
    return float(match.group()) if match else float("inf")


def _occurrences(name: str, text: str) -> list[int]:
    """
    Every place a concept appears in a document's normalised text, in order.

    Tries the name, then its singular or plural, then its longest word, and
    uses the first of those that appears at all, since extracted names are
    often a rewording ("data structure augmentation" for a lecture that says
    "augment"). The longest word is the most specific one and least likely to
    hit an unrelated mention.
    """
    key = _normalise_relation(name)
    if not key:
        return []
    variants = [key, key[:-1] if key.endswith("s") else key + "s"]
    words = [w for w in re.split(r"[^a-z0-9]+", key) if len(w) >= 5]
    if words:
        variants.append(max(words, key=len))
    for variant in variants:
        hits = [m.start() for m in re.finditer(rf"\b{re.escape(variant)}", text)]
        if hits:
            return hits
    return []


def _first_position(name: str, text: str) -> Optional[int]:
    """Where a concept first appears in a document's normalised text, or None."""
    hits = _occurrences(name, text)
    return hits[0] if hits else None


def _content_body(text: str) -> str:
    """
    A document's own content, normalised for matching: the text with running
    heads, the title and author lines, the overview list and the reference
    list taken out, by the same rules the must-cover picker uses
    (key_sentences.content_lines).

    Left in, the running head and title put "binary search tree" at position 0
    of Lecture 5, and the overview list put "avl tree" at 2% of Lecture 6,
    ahead of where either lecture teaches them.
    """
    return _normalise_relation(key_sentences.content_text(text))


def concept_occurrences(concept_names, sources_by_concept, doc_order, texts):
    """
    Everywhere each concept appears in the source's content:
    {name: [(document index, fraction of the way through it), ...]}, sorted.

    Document index follows `doc_order`; fractions are of _content_body, so
    they line up with the must-cover picks, which are placed in the same
    text. A concept found in no text is left out.
    """
    rank = {doc: i for i, doc in enumerate(doc_order)}
    bodies = {doc: _content_body(t) for doc, t in texts.items()}
    out = {}
    for name in concept_names:
        spots = []
        for doc in sources_by_concept.get(name, ()):
            if doc not in rank:
                continue
            body = bodies.get(doc, "")
            spots += [(rank[doc], pos / max(len(body), 1))
                      for pos in _occurrences(name, body)]
        if spots:
            out[name] = sorted(spots)
    return out


def concept_positions(occurrences):
    """
    Where each concept first appears: {name: (document index, fraction)}, the
    earliest of its occurrences. Used by the plan prompt for grouping and by
    order_sections for the final order.
    """
    return {name: spots[0] for name, spots in occurrences.items()}


def order_sections(sections, positions):
    """
    Reorders sections by where their material first appears in the source:
    document order first, then position within the document.

    The model orders "foundations to consequences" from relation pairs alone
    and has no idea where anything sits in the lecture, so on Lectures 5 + 6
    it put the runway reservation problem, which opens Lecture 5 and motivates
    everything after it, last. A lecture already orders its material for
    teaching, so the source order is used instead.

    A section sits at its earliest concept. Concepts found in no text sort
    after everything placed; ties keep the model's order.
    """
    unplaced = (float("inf"), float("inf"))
    placed = [(min((positions.get(n, unplaced) for n in s["concepts"]), default=unplaced), i, s)
              for i, s in enumerate(sections)]
    return [s for _, _, s in sorted(placed, key=lambda t: (t[0], t[1]))]


# ---------------------------------------------------------------------------
# must-cover sentences
#
# key_sentences picks the sentences a document's note has to cover, without an
# LLM. Here they are cached, assigned to sections at plan time and checked
# against what was written. All embedding goes through the engine's own
# SentenceTransformer, so no second model is loaded.
# ---------------------------------------------------------------------------

# A pick is tied between sections whose nearest concept sits within this
# fraction of the document of the closest one; cosine to the sections' concept
# names breaks the tie.
_ASSIGN_TIE = 0.02

# Coverage bands: a pick's best cosine against the written sentences. Set from
# one hand-checked Lecture 6 section, so they are a starting point to tune.
COVERAGE_COVERED = 0.70
COVERAGE_WEAK = 0.55

_KEYSENT_LOCK = asyncio.Lock()


def _encoder():
    """The engine's embedding model as encode(list[str]) -> normalised
    vectors, or None when no engine or model is loaded."""
    model = getattr(_engine(), "model", None)
    if model is None or not hasattr(model, "encode"):
        return None
    return lambda texts: model.encode(list(texts), normalize_embeddings=True,
                                      show_progress_bar=False)


async def document_picks(texts: dict[str, str]) -> dict[str, list[dict]]:
    """Must-cover picks for each document, computed once per document text."""
    encode = _encoder()
    if encode is None or not texts:
        return {}

    def work():
        cache = _load_cache(KEYSENT_CACHE_FILE)
        out, changed = {}, False
        for doc, text in texts.items():
            key = f"{key_sentences.VERSION}|{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
            if key not in cache:
                cache[key] = key_sentences.pick(text, encode)
                changed = True
            out[doc] = cache[key]
        if changed:
            _save_cache(KEYSENT_CACHE_FILE, cache)
        return out

    async with _KEYSENT_LOCK:
        return await asyncio.to_thread(work)


async def assign_must_cover(sections, doc_order, occurrences, texts) -> list[list[str]]:
    """
    Each section's must-cover sentences, in source order.

    Every pick goes to exactly one section: the one with a concept occurring
    closest to it in the same document. Distance is to the concept's nearest
    occurrence, not its first: a lecture names its topics early, in the
    overview or the opening motivation, and teaches them later, so measuring
    from the first mention sent Lecture 6's AVL insert and AVL sort picks to
    whichever section had a concept mentioned later on. Sections within
    _ASSIGN_TIE of the closest are tied, and the pick goes to whichever one's
    concept names it is most similar to. A pick whose document holds none of
    the sections' concepts is placed on similarity alone.
    """
    empty = [[] for _ in sections]
    picks = await document_picks(texts)
    encode = _encoder()
    if not picks or encode is None or not sections:
        return empty

    rank = {doc: i for i, doc in enumerate(doc_order)}
    flat = []
    for doc, items in picks.items():
        if doc not in rank:
            continue
        body = _content_body(texts.get(doc, ""))
        for p in items:
            # Placed in the same text concept_occurrences measured, so the
            # two fractions compare; the raw fraction is the fallback.
            key = _normalise_relation(p["text"])[:40]
            at = body.find(key) if key else -1
            frac = at / max(len(body), 1) if at >= 0 else p["fraction"]
            flat.append((rank[doc], frac, p["text"]))
    if not flat:
        return empty

    def work():
        return (encode([t for _, _, t in flat]),
                encode([", ".join(s["concepts"]) for s in sections]))

    pick_emb, section_emb = await asyncio.to_thread(work)

    out: list[list] = [[] for _ in sections]
    for (doc, frac, text), vec in zip(flat, pick_emb):
        dist = []
        for s in sections:
            near = [abs(f - frac) for c in s["concepts"]
                    for d, f in occurrences.get(c, ()) if d == doc]
            dist.append(min(near) if near else float("inf"))
        best = min(dist)
        tied = (range(len(sections)) if best == float("inf")
                else [i for i, d in enumerate(dist) if d <= best + _ASSIGN_TIE])
        choice = max(tied, key=lambda i: float(vec @ section_emb[i]))
        out[choice].append((doc, frac, text))
    return [[t for _, _, t in sorted(group)] for group in out]


def _written_sentences(body: dict) -> list[str]:
    """What a written section says, sentence by sentence, for coverage: the
    prose blocks plus each table row as one line."""
    out = [s.strip() for p in _prose_of(body) for s in _SENTENCE.findall(p)
           if len(s.split()) >= 3]
    for term in body.get("terms") or []:
        out.append(f"{term.get('term', '')}: {term.get('definition', '')}")
    table = body.get("table") or {}
    for row in table.get("rows") or []:
        out.append(" | ".join(str(cell) for cell in row))
    return out


async def measure_coverage(picks: list[str], body: dict) -> Optional[dict]:
    """
    How well a written section covers its must-cover picks: each pick's best
    cosine against the written sentences, banded into covered, weak and
    missing. Recorded only — nothing is rewritten or retried on it.
    """
    encode = _encoder()
    if not picks or encode is None:
        return None
    written = _written_sentences(body)
    if written:
        def work():
            return (encode(picks) @ encode(written).T).max(axis=1)
        best = [float(b) for b in await asyncio.to_thread(work)]
    else:
        best = [0.0] * len(picks)
    covered = sum(b >= COVERAGE_COVERED for b in best)
    weak = sum(COVERAGE_WEAK <= b < COVERAGE_COVERED for b in best)
    return {
        "total": len(picks),
        "covered": covered,
        "weak": weak,
        "missing": len(picks) - covered - weak,
        "missing_picks": [p for p, b in zip(picks, best) if b < COVERAGE_WEAK],
    }


def _document_order(user_id: int, document_ids: list[int]) -> list[str]:
    """The note's filenames in teaching order: by week, then by filename read
    naturally, so Lecture 5 comes before Lecture 6 and Lecture 10."""
    unique = list(dict.fromkeys(document_ids))
    placeholders = ",".join("?" for _ in unique)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT filename, week FROM documents WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *unique),
        ).fetchall()
    rows = sorted(rows, key=lambda r: (_week_number(r["week"]), _natural_key(r["filename"])))
    return list(dict.fromkeys(r["filename"] for r in rows))


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
            "must_cover": list(section.get("must_cover") or []),
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
# v6: Lecture 5's sections overlapped and overreached. The whole lecture fits
# in one chunk, so every section was written from the same text and covered
# the same material. The prompt now lists the concepts other sections cover
# and asks for them to be left out; extracts are limited to the note's own
# documents; a listed connection the extracts do not support is dropped; and
# numbers lifted from a figure are no longer worked into an example.
#
# v7: Lecture 6 came back as one 13-concept section squeezed into four
# paragraphs, missing the height derivation, AVL sort and both heap vs AVL
# tables, and it hedged facts the text states ("x = 29: left-left case" became
# "might be a left-left case"). The figure rule now forbids only values no line
# states; a table whose layout was lost may be rebuilt when every cell appears
# verbatim; the paragraph count follows the concept count; and the concept list
# is no longer treated as the limit of what to cover.
#
# v8: relations without a summary are dropped from the checklist and the
# label check, and a comparison must give each option's cost separately. A
# Lecture 5 note said sorted lists, sorted arrays, heaps and dicts all fall
# short on "the O(n) k-minute check"; the sorted array's check is O(1).
#
# v9: each section carries MUST COVER sentences picked from the source by
# key_sentences, and coverage of them is measured after writing and stored on
# the body.
#
# v10: must-cover picks are assigned by each concept's nearest occurrence in
# the content, not its first mention, and the picker skips overview lists.
#
# v11: the paragraph count is also capped by the section's source length, and
# no paragraph may restate an earlier one. A 2-page economics handout was
# asked for 6-7 paragraphs and explained equilibrium three times.
#
# v12: examples the extracts give in passing are kept, and the source's
# grouping is kept. The economics note dropped all three of its examples once
# it was shortened, and called a market economy "also known as a capitalistic
# economy" where the source lists capitalistic and command as its two kinds.
#
# Each bump changes the cache key, so bodies written under an older prompt are
# never served.
_WRITE_PROMPT_VERSION = "notes-write-v12"

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
    "and definition comes out of them. Invent nothing they do not support. "
    "Where they only say a topic comes later ('next lecture'), say only that; "
    "do not supply what it will show.\n"
    "\n"
    "MUST COVER lists sentences picked from the source as this section's key "
    "points. Cover each one, in your own words rather than copied. They are "
    "taken from the extracts, so they are material, but they override "
    "nothing: every other rule here still applies to them.\n"
    "\n"
    "THE CONCEPT LIST IS NOT EXHAUSTIVE. It was extracted automatically and "
    "misses things. Also cover what the extracts teach that no listed concept "
    "names — a derivation, a named algorithm, a comparison — unless it belongs "
    "to a concept listed under COVERED IN OTHER SECTIONS.\n"
    "\n"
    "OTHER SECTIONS OF THE NOTE cover the concepts listed under COVERED IN "
    "OTHER SECTIONS. The extracts may discuss them too, because they come from "
    "the same pages. Leave them out: do not explain, define, tabulate or give "
    "an example of them. Name one only where a sentence about this section's "
    "own concepts cannot be written without it.\n"
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
    "- The list was extracted automatically and can be wrong. A listed "
    "connection the extracts do not support is left out, not explained.\n"
    "\n"
    "Fill these blocks:\n"
    "- lead: ONE sentence, written as an assertion, giving the single thing a "
    "reader must hold onto after this section. A claim, not a description of "
    "what follows. It must NOT begin with 'This section', 'This part', 'Here "
    "we', 'In this' or any other announcement, and must not restate the "
    "title.\n"
    "- body: as many paragraphs of explanation as the request asks for, in "
    "the order a reader "
    "should meet the ideas. Each paragraph is a heading plus a list of spans, "
    "and the spans joined in order are the paragraph's prose — so put the "
    "spaces in, and do not repeat text between spans.\n"
    "  * Every paragraph adds something new. Never restate a point the lead "
    "or an earlier paragraph already made, even in other words.\n"
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
    "\n"
    "COMPARISONS. When the extracts compare several structures or options, "
    "give each one's cost separately, as the extracts state it for that one. "
    "Never merge several into one sentence with a shared cost ('sorted lists, "
    "sorted arrays and heaps all need an O(n) check') unless the extracts give "
    "that cost for every one of them.\n"
    "\n"
    "EXAMPLES IN PASSING. When the extracts give a concrete example for a "
    "point ('e.g. eBay auctions', 'such as ...'), keep it, briefly, beside the "
    "point it illustrates. A shorter note drops explanation, not examples.\n"
    "\n"
    "KEEP THE SOURCE'S STRUCTURE. Group things the way the extracts do. If "
    "they present A and B as kinds or parts of C, write them as kinds or "
    "parts of C: never describe A as another name for C, and never promote a "
    "part to the whole or merge two siblings into one.\n"
    "\n"
    "FIGURES AND TABLES LOSE THEIR LAYOUT IN EXTRACTION. Their labels arrive "
    "as short lines, often one value per line.\n"
    "- A line that states something in words is a fact, even inside a figure: "
    "'x = 29: left-left case', 'Insert(23)', 'if x's right child is "
    "right-heavy or balanced: follow steps in Fig. 5', 'max. h ≈ 1.440 lg n'. "
    "State it plainly. Do not hedge it with 'might', 'could' or 'may'.\n"
    "- What is forbidden is a value or a calculation that no line states: "
    "assigning bare numbers to tree nodes, working out a sum, or filling in a "
    "step the figure only drew.\n"
    "- A table whose layout was lost may be rebuilt ONLY when every cell value "
    "appears verbatim in the extracts and the row and column pattern is "
    "unambiguous — row labels, column headers and values repeating in a clear "
    "order. If any cell is uncertain, set table to null and describe the "
    "comparison in prose instead.\n"
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


def _write_key(section: dict, other_concepts=()) -> str:
    """
    Cache key for one section's prose: prompt version, model, and what the
    section is. Identity is the title, concepts and relations recorded at plan
    time — regenerate the same plan and the same section hits the same entry.
    The other sections' concepts are part of it too, since the prompt asks for
    them to be left out.

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
        "others": sorted(other_concepts),
        "must_cover": list(section.get("must_cover") or []),
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"{_WRITE_PROMPT_VERSION}|{GEMINI_MODEL}|section:{digest}"


async def gather_chunks(concept_names, documents=None, retrieve=None) -> list[dict]:
    """
    Source text behind a section, one retrieval per concept name.

    retrieve_chunks embeds the query and hits the vector store, so it blocks;
    all of the calls for one section share a single thread hop. Chunks are
    deduplicated on their text, because neighbouring concepts routinely
    retrieve the same passage, and kept whole — the write step is the one place
    that wants the full chunk rather than a snippet.

    `documents` limits the hits to the note's own files. Without it a section
    picked up other lectures in the store (Lecture 5 notes cited Lecture 6).
    """
    if retrieve is None:
        engine = _engine()
        retrieve = getattr(engine, "retrieve_chunks", None)
    if retrieve is None:
        return []

    def collect():
        found, seen = [], set()
        for name in concept_names:
            try:
                hits = retrieve(name, top_k=_RETRIEVE_POOL if documents else _CHUNKS_PER_CONCEPT)
            except Exception as exc:            # a broken store is not a 500
                print(f"Notes write: chunk retrieval failed for {name!r} — {exc}")
                continue
            if documents:
                hits = [h for h in hits or [] if h.get("source") in documents]
            for hit in (hits or [])[:_CHUNKS_PER_CONCEPT]:
                # Normalised on the way in as well as out, so the model never
                # sees the ligatures the PDFs carry and cannot copy them back.
                text = normalise_unicode((hit.get("text") or "").strip())
                if text and text not in seen:
                    seen.add(text)
                    found.append({**hit, "text": text})
        found.sort(key=lambda h: h.get("similarity") or 0, reverse=True)
        return found[:_MAX_WRITE_CHUNKS]

    return await asyncio.to_thread(collect)


# Body paragraphs per section: about one per two to three concepts, at least
# two, at most seven. A fixed two to four squeezed Lecture 6's 13 concepts into
# four paragraphs and the derivations were what got cut.
MIN_PARAGRAPHS = 2
MAX_PARAGRAPHS = 7


# The paragraph count is also capped by how much source text the section is
# written from: about one paragraph per this many characters of extracts. A
# two-page economics handout (about 2,500 characters of bullets) holding 17
# concepts was asked for six to seven paragraphs and repeated its equilibrium
# explanation three times to fill them. Lecture 6 (4,890 characters) still
# gets the full seven.
PARAGRAPH_SOURCE_CHARS = 650


def paragraph_range(n_concepts: int, source_chars: Optional[int] = None) -> tuple[int, int]:
    """
    (fewest, most) body paragraphs for a section of `n_concepts` written from
    `source_chars` characters of extracts: the smaller of the concept-based
    range and the length-based cap, never below MIN_PARAGRAPHS.
    """
    least = min(MAX_PARAGRAPHS, max(MIN_PARAGRAPHS, -(-n_concepts // 3)))
    most = min(MAX_PARAGRAPHS, max(least, -(-n_concepts // 2)))
    if source_chars is not None:
        cap = max(MIN_PARAGRAPHS, source_chars // PARAGRAPH_SOURCE_CHARS)
        least, most = min(least, cap), min(most, cap)
    return least, most


def build_write_prompt(section: dict, chunks: list[dict], other_concepts=()) -> str:
    """
    The material half of the write prompt.

    Extracts come first and are labelled as the thing being written from;
    relations follow as a coverage checklist. The first version led with the
    relations, and the model treated whatever came first as its material —
    it wrote the edge list back as prose and barely touched the source text.
    Order carries that weight, so the sections are ordered deliberately.
    """
    least, most = paragraph_range(len(section["concepts"]),
                                  sum(len(c["text"]) for c in chunks))
    count = (f"exactly {least}" if least == most else f"between {least} and {most}")
    lines = [f"SECTION TITLE: {section['title']}", "",
             "CONCEPTS THIS SECTION COVERS:", ", ".join(section["concepts"]), "",
             f"PARAGRAPHS: write {count} body paragraphs.", ""]
    if other_concepts:
        lines += ["COVERED IN OTHER SECTIONS — leave these out:",
                  ", ".join(sorted(other_concepts)), ""]

    lines.append("SOURCE EXTRACTS — write the section from these:")
    if chunks:
        for i, chunk in enumerate(chunks, 1):
            where = chunk.get("source") or "unknown document"
            lines += [f"[{i}] from {where}", chunk["text"], ""]
    else:
        lines += ["(none retrieved)", ""]

    if section.get("must_cover"):
        lines += [
            "MUST COVER — the source's key points for this section. Cover each "
            "one in your own words. They do not override the rules: nothing "
            "beyond the extracts, and nothing that belongs to COVERED IN OTHER "
            "SECTIONS.",
        ]
        lines += [f"- {p}" for p in section["must_cover"]]
        lines.append("")

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


def _mentions(name: str, text: str) -> bool:
    """Whether a normalised sentence names a concept, allowing a plural."""
    key = _normalise_relation(name)
    return bool(key) and re.search(rf"\b{re.escape(key)}(?:e?s)?\b", text) is not None


def find_label_violations(body: dict, relations: list[dict]) -> list[tuple[str, str]]:
    """
    Sentences that restate one of this section's own relations by its label.

    The ban is in the prompt and the model half-keeps it, so it is checked
    here too. A sentence counts only when it holds the label AND names both
    ends of an edge carrying it. The label alone is ordinary English: Lecture 5
    was flagged for "inserting in sorted order can cause the tree to resemble a
    linked list", which restates nothing, and the flag cost a rewrite call.

    Returns (label, sentence) pairs, at most one per label, so the rewrite
    instruction names each problem once.
    """
    patterns: dict[str, re.Pattern] = {}
    ends: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in relations or []:
        label = _normalise_relation(edge.get("relation"))
        pattern = patterns.get(label) or _label_pattern(label)
        if pattern is None:
            continue
        patterns[label] = pattern
        ends[label].append((edge.get("from") or "", edge.get("to") or ""))

    found: list[tuple[str, str]] = []
    seen = set()
    for paragraph in _prose_of(body):
        for sentence in _SENTENCE.findall(paragraph):
            text = sentence.strip()
            if not text:
                continue
            lowered = text.lower()
            normalised = _normalise_relation(text)
            for label, pattern in patterns.items():
                if label in seen or not pattern.search(lowered):
                    continue
                if any(_mentions(a, normalised) and _mentions(b, normalised)
                       for a, b in ends[label]):
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


def _retry_delay_seconds(exc: Exception) -> Optional[float]:
    """
    The wait the server itself asked for, in seconds, or None.

    A quota error carries a google.rpc.RetryInfo detail saying when the
    window reopens ("retryDelay": "23s"). Honouring it beats guessing: the
    free tier limits requests per minute, so a blind backoff shorter than the
    window spends another request on a refusal, and one longer than it waits
    for nothing.

    Read off exc.details, which the SDK sets to the parsed error body, so
    this needs no import from google.genai.
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    inner = details.get("error")
    items = (inner if isinstance(inner, dict) else details).get("details")
    for item in items or []:
        if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("RetryInfo"):
            continue
        raw = item.get("retryDelay")
        if isinstance(raw, (int, float)):
            return float(raw)
        # protobuf Duration in JSON: a number of seconds with an "s" suffix.
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(raw or "").strip())
        if match:
            return float(match.group(1))
    return None


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


async def write_body(section: dict, documents=None,
                     other_concepts=(), chunk_retriever=None) -> tuple[Optional[dict], Optional[float]]:
    """
    Writes one section's prose. Consults the write cache first and writes back
    on success.

    `documents` are the note's files, which extracts are limited to;
    `other_concepts` are what the note's other sections cover.

    Returns (body, retry_after) — the block structure, or None with the wait
    the model server asked for when it refused on quota.
    """
    # Relations without a summary are left out of the checklist and the label
    # check, as they are left out of the Connections block: a bare label from
    # extraction or fusion is often wrong ("abstract data type is a type of
    # data structure"), and the checklist would ask the writer to explain it.
    section = {**section, "relations": [
        e for e in section["relations"] if str(e.get("summary") or "").strip()
    ]}
    cache_key = _write_key(section, other_concepts)
    cached = _load_write_cache().get(cache_key)
    if isinstance(cached, dict) and cached.get("lead"):
        return cached, None

    client = _gemini_client()
    if client is None:
        return None, None

    chunks = await gather_chunks(section["concepts"], documents, retrieve=chunk_retriever)
    if not chunks:
        print(f"Notes write: no source text retrieved for section {section['id']} "
              f"({len(section['concepts'])} concepts) — nothing to write from.")
        return None, None

    prompt = build_write_prompt(section, chunks, other_concepts)
    body, retry_after = await _generate_body(client, prompt)
    if body is None:
        return None, retry_after

    # The label ban is a prompt rule the model only half-keeps, so it is
    # enforced here: one rewrite, with the offending sentences quoted back.
    violations = find_label_violations(body, section["relations"])
    if violations:
        print(f"Notes write: section {section['id']} used "
              f"{len(violations)} relation label(s) as words "
              f"({', '.join(l for l, _ in violations)}) — asking for a rewrite.")
        second, _ = await _generate_body(client, prompt + _rewrite_instruction(violations))
        if second is not None:
            # Taken either way. A second pass that still slips is better than
            # a loop, and the count below records what got through.
            body = second
            left = find_label_violations(body, section["relations"])
            print(f"Notes write: rewrite {'clean' if not left else 'still using ' + ', '.join(l for l, _ in left)}.")
        else:
            print("Notes write: rewrite call failed — keeping the first answer.")

    coverage = await measure_coverage(list(section.get("must_cover") or []), body)
    if coverage is not None:
        body["coverage"] = coverage
        logger.info("notes_write_coverage section=%s covered=%d weak=%d missing=%d total=%d",
                    section["id"], coverage["covered"], coverage["weak"],
                    coverage["missing"], coverage["total"])

    # Whether a second Gemini call was issued for this section, not whether
    # its answer was kept — a rewrite that then failed still spent the call.
    # Recorded because label_violations alone cannot say: it is measured
    # after the rewrite, so an empty list means either a clean first draft
    # or a rewrite that worked. Sections per minute against a request quota
    # depends on which, so it goes on disk rather than only into the log.
    body["rewritten"] = bool(violations)
    body["label_violations"] = [
        label for label, _ in find_label_violations(body, section["relations"])
    ]
    body["sources"] = sorted({c.get("source") for c in chunks if c.get("source")})
    body["chunks_used"] = len(chunks)

    await _store_written(cache_key, body)
    return body, None


def _usage(response) -> dict:
    """
    What the call actually spent, as far as the response reports it.

    thoughts is the one that matters for batching. gemini-2.5-flash reasons
    before it answers and no thinking_config is set here, so whatever it
    spends thinking comes out of the same 65,536-token response budget the
    section bodies have to fit in. Batching N sections into one call cannot
    be sized without it, and it is free to read — the number rides along on
    a response already paid for.
    """
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return {}
    fields = ("prompt_token_count", "candidates_token_count",
              "thoughts_token_count", "total_token_count")
    return {f: getattr(meta, f, None) for f in fields}


async def _generate_body(client, prompt: str) -> tuple[Optional[dict], Optional[float]]:
    """
    One Gemini call for a section body, retried on transient failures and
    parsed against the schema.

    Returns (body, retry_after). `body` is the block dict, or None if the call
    could not be made. `retry_after` is the wait the server itself asked for
    when it refused on quota, so the route can hand a real number to the
    client rather than have it guess one.
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
    waited = 0.0
    for attempt in range(_WRITE_MAX_ATTEMPTS):
        last = attempt == _WRITE_MAX_ATTEMPTS - 1
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(call), timeout=_WRITE_TIMEOUT_SECONDS
            )
            break
        except asyncio.TimeoutError:
            print(f"Notes write: call exceeded {_WRITE_TIMEOUT_SECONDS}s — giving up.")
            return None, None
        except Exception as exc:
            if last or not _is_transient(exc):
                print(f"Notes write: Gemini call failed — {type(exc).__name__}: {exc}")
                return None, _retry_delay_seconds(exc)
            asked = _retry_delay_seconds(exc)
            delay = asked if asked is not None else _WRITE_RETRY_BASE_SECONDS * (3 ** attempt)
            if waited + delay > _WRITE_RETRY_BUDGET_SECONDS:
                # The exception is printed whole here: its details carry the
                # QuotaFailure, whose quotaId is the only thing that says
                # which quota was hit — per-minute or per-day.
                print(f"Notes write: {type(exc).__name__} on attempt "
                      f"{attempt + 1}/{_WRITE_MAX_ATTEMPTS}, next wait {delay}s would pass the "
                      f"{_WRITE_RETRY_BUDGET_SECONDS}s retry budget — giving up. {exc}")
                return None, asked
            source = "server asked for" if asked is not None else "backing off"
            print(f"Notes write: {type(exc).__name__} on attempt "
                  f"{attempt + 1}/{_WRITE_MAX_ATTEMPTS} — {source} {delay}s.")
            await asyncio.sleep(delay)
            waited += delay
    if response is None:
        return None, None

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        # The schema is enforced server-side, so this is unusual; parse the
        # text rather than lose the answer to it.
        try:
            parsed = _WriteBody.model_validate_json(response.text or "")
        except (ValidationError, AttributeError) as exc:
            print(f"Notes write: unusable response — {exc}")
            return None, None

    body = normalise_unicode(
        parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
    )

    used = _usage(response)
    if used:
        logger.debug(
            "notes_write_usage in=%s out=%s thoughts=%s total=%s",
            used.get("prompt_token_count"), used.get("candidates_token_count"),
            used.get("thoughts_token_count"), used.get("total_token_count"),
        )
        # Written onto the body as well as logged. The log lives in a terminal
        # nobody kept; the cache is on disk and can be read back later.
        body["usage"] = used

    return body, None


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

    source_cap = source_section_cap(filenames)
    logger.info("notes_plan concepts=%d max_sections=%s",
                len(concept_names), source_cap if source_cap is not None else "unknown")

    doc_order = _document_order(user["id"], body.document_ids)
    texts = document_texts(filenames)
    occurrences = concept_occurrences(concept_names, sources_by_concept, doc_order, texts)
    positions = concept_positions(occurrences)

    trace: list[dict] = []
    sections = await plan_sections(
        concept_names, edges,
        _plan_key(_PLAN_PROMPT_VERSION, body.document_ids, body.days),
        trace, source_cap=source_cap, locality=(doc_order, positions),
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

    sections = order_sections(sections, positions)

    must_cover = await assign_must_cover(sections, doc_order, occurrences, texts)
    for section, picks in zip(sections, must_cover):
        section["must_cover"] = picks
    logger.info("notes_plan sections=%d must_cover=%d per_section=%s",
                len(sections), sum(len(p) for p in must_cover),
                [len(p) for p in must_cover])

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


# Generations running right now, keyed by section id.
#
# Starlette does not cancel a route handler when the browser goes away —
# measured on this stack (starlette 1.5.0, uvicorn 0.52.1): a client that
# disconnects two seconds in leaves the handler running to completion. So a
# reader who opens a section, navigates off and comes back would otherwise
# have two generations of the same section in the air, both billed. Holding
# the task here means the second request joins the first instead of starting
# its own, whatever the client does.
#
# The task is created rather than awaited inline for the same reason: it must
# outlive the request that started it, so a reader who leaves does not take
# the work away from one who stayed.
_WRITES_IN_FLIGHT: dict[int, asyncio.Task] = {}


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

    task = _WRITES_IN_FLIGHT.get(section_id)
    if task is None:
        others = [c for s in note["sections"] if s is not section for c in s["concepts"]]
        task = asyncio.create_task(write_body(section, set(note["documents"]), others))
        _WRITES_IN_FLIGHT[section_id] = task
        task.add_done_callback(lambda _t: _WRITES_IN_FLIGHT.pop(section_id, None))

    # Awaiting a Task does not tie its life to this request: if this caller is
    # cancelled the generation carries on for whoever else is waiting.
    body, retry_after = await task

    if body is None:
        detail = "Could not write this section just now. Please try again."
        headers = None
        if retry_after:
            wait = int(retry_after) + 1
            detail = (f"The note writer is out of quota just now. "
                      f"Try again in {wait} seconds.")
            # Standard header, so the client waits the length the model server
            # actually named instead of guessing a cooldown.
            headers = {"Retry-After": str(wait)}
        raise HTTPException(status_code=503, detail=detail, headers=headers)

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
