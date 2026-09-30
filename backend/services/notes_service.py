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

# Sections per document. A document is cut into contiguous sections of about
# SECTION_CHARS characters; one under SINGLE_SECTION_BELOW stays whole. No
# document gets more than MAX_SECTIONS_PER_DOCUMENT — a longer one gets longer
# sections instead — and no note more than MAX_NOTE_SECTIONS, reached by
# growing the section size for every document alike. The earlier rule (one
# section per document up to 8,000 characters) left Lecture 5's scheduling
# problem and its BST material in one section.
SECTION_CHARS = 3500
SINGLE_SECTION_BELOW = 4500
MAX_SECTIONS_PER_DOCUMENT = 6
MAX_NOTE_SECTIONS = 10


def document_section_count(chars: int, size: float = SECTION_CHARS) -> int:
    """
    How many sections one document's text is worth at a section size of
    `size` characters: one below SINGLE_SECTION_BELOW (scaled with the size),
    else about chars / size, at least two and at most
    MAX_SECTIONS_PER_DOCUMENT. At the cap the sections simply get longer.
    """
    if chars < SINGLE_SECTION_BELOW * size / SECTION_CHARS:
        return 1
    return min(MAX_SECTIONS_PER_DOCUMENT, max(2, round(chars / size)))


def note_section_counts(lengths: dict[str, int]) -> dict[str, int]:
    """
    Sections per document for one note, {document: count}. Past
    MAX_NOTE_SECTIONS in total, the section size grows for every document
    alike, so each one's share shrinks in proportion, until the note fits or
    every document is down to one section.
    """
    size = float(SECTION_CHARS)
    counts = {d: document_section_count(n, size) for d, n in lengths.items()}
    while sum(counts.values()) > MAX_NOTE_SECTIONS and any(c > 1 for c in counts.values()):
        size *= 1.05
        counts = {d: document_section_count(n, size) for d, n in lengths.items()}
    return counts


# A section is written from the concepts first taught in its stretch of one
# document. Grouping is deterministic (group_by_document); the one plan call
# only names the sections. Its answer is small — a few titles — so the call
# gets a small budget.
_TITLE_MAX_WORDS = 8
_TITLES_MAX_TOKENS = 400

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


def _passed_quizzes(user_id) -> set[str]:
    """Section quiz ids this user has passed, from the saved attempts."""
    from services.quiz_service import passed_quiz_ids
    return passed_quiz_ids(user_id)


def _is_done(section: dict, passed) -> bool:
    """A section is done when it was marked so or its quiz has a saved passing
    attempt. The second survives a reload and a restart; a manual untick of a
    passed section is the client's to hold."""
    quiz_id = (section.get("body") or {}).get("quiz_id")
    return bool(section["done"] or (quiz_id and quiz_id in passed))


def _note_public(note: dict) -> dict:
    """Serialiser, in the shape of repositories.documents.document_public."""
    passed = _passed_quizzes(note["user_id"])
    return {
        "id": note["id"],
        "title": note["title"],
        "kind": note["kind"],
        "days": note["days"],
        "document_ids": note["document_ids"],
        "documents": note["documents"],
        "created_at": note["created_at"],
        "total": len(note["sections"]),
        "done": sum(1 for s in note["sections"] if _is_done(s, passed)),
        "sections": [_section_public(s, passed) for s in note["sections"]],
    }


def _served_body(body):
    """The body as it leaves the backend: its quiz shuffled (quiz_service.
    shuffle_quiz) and the pass mark for its length attached. The stored body,
    cached ones included, keeps the order the model wrote."""
    if not body or not body.get("quiz"):
        return body
    from services.quiz_service import pass_mark, shuffle_quiz
    return {**body, "quiz": shuffle_quiz(body["quiz"]), "quiz_pass_mark": pass_mark(len(body["quiz"]))}


def _section_public(section: dict, passed=frozenset()) -> dict:
    return {
        "id": section["id"],
        "note_id": section["note_id"],
        "index": section["index"],
        "day": section["day"],
        "title": section["title"],
        "concepts": section["concepts"],
        "relations": section["relations"],
        "sources": section["sources"],
        # The section's must-cover source sentences. The UI no longer reads
        # them (the per-question Review link is gone); kept as data.
        "must_cover": section.get("must_cover") or [],
        "body": _served_body(section["body"]),
        "done": _is_done(section, passed),
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


# Labels that set two ideas against each other. A subset of _SYMMETRIC: these
# are the pairs a student confuses, so they make the best wrong options.
_CONTRAST = ("contrast*", "differ*", "distinguish*", "unlike", "versus", "vs",
             "opposite*", "opposed to", "as opposed to", "alternative to")


def is_contrast(relation: str) -> bool:
    return _match(_normalise_relation(relation), _CONTRAST)


# Size of a section's distractor pool. Enough to give five questions three
# wrong options each some choice, without the pool outweighing the section.
_MAX_DISTRACTOR_POOL = 12


def section_neighbours(names, edges: list[dict]) -> list[dict]:
    """
    Concepts one link outside a section: the distractor pool for its quiz.

    Each entry names the outside concept, the section concepts it links to,
    and which of those links are contrasts. Contrasted concepts come first,
    then the most connected — a concept tied to several of the section's
    ideas is the likelier confusion.
    """
    inside = set(names)
    links: dict[str, set] = defaultdict(set)
    contrasts: dict[str, set] = defaultdict(set)
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if not a or not b or (a in inside) == (b in inside):
            continue
        outside, anchor = (b, a) if a in inside else (a, b)
        links[outside].add(anchor)
        if is_contrast(e.get("relation")):
            contrasts[outside].add(anchor)
    ranked = sorted(links, key=lambda n: (-len(contrasts[n]), -len(links[n]), n))
    return [{"name": n, "links": sorted(links[n]), "contrasts": sorted(contrasts[n])}
            for n in ranked[:_MAX_DISTRACTOR_POOL]]


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
# v7: one section per document by default (document_section_count), not one
# per 2,500 characters overall, so the requested count changed.
# v8: grouping is deterministic — each document's concepts, in contiguous
# ranges by source position when the document is split — and the one call
# only writes titles. Asked to group, the model grouped by topic: Lecture 5's
# BST and Rank material went into a section with all of Lecture 6.
# v9: sections of about 3,500 characters (a document under 4,500 stays
# whole), at most 6 per document and 10 per note; must-cover picks stay in
# their own document's sections.
# v10: must-cover picks go to the section whose text range contains them
# (concept distance only for a pick that cannot be located), and a concept
# the text never names joins the section holding most of its neighbours
# rather than the document's last.
_PLAN_PROMPT_VERSION = "notes-plan-v10"

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
    """How many sections the documents' text is worth in all
    (note_section_counts), or None when the engine holds no chunks for
    them."""
    texts = {d: len(t) for d, t in document_texts(filenames).items() if t}
    if not texts:
        return None
    return sum(note_section_counts(texts).values())


_TITLES_SYSTEM = (
    "You name the sections of a student's revision note. Each section is given "
    "with the document it comes from and the concepts it covers, in the order "
    "that document teaches them. The sections are already decided; you only "
    "write their titles.\n"
    "Rules:\n"
    "- One title per section, in the order given.\n"
    f"- A title is a short noun phrase naming what the section teaches, at most "
    f"{_TITLE_MAX_WORDS} words. Do not number it, and do not use the file name.\n"
    "- A title may only name concepts in that section. Do not name a topic, "
    "technique or structure that is not in its concept list.\n"
    "- Sections from the same document must get different titles.\n"
    'Respond ONLY as JSON: {"titles": ["...", "..."]}.'
)


def build_titles_prompt(sections) -> str:
    """The user half of the titles call: each section's document and its
    concepts in source order."""
    lines = [f"{len(sections)} section{'s' if len(sections) != 1 else ''} to title.", ""]
    for i, section in enumerate(sections, 1):
        where = section.get("document") or "several documents"
        lines += [f"SECTION {i} — from {where}:", ", ".join(section["concepts"]), ""]
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# response validation
# ---------------------------------------------------------------------------

class _TitlesResponse(BaseModel):
    titles: list[str] = Field(min_length=1)


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
    """Write a cache; best-effort, a failure is not fatal. Written to a
    temporary file and swapped in, so a reader never loads half a file as an
    empty cache and an interrupted write keeps the previous one."""
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
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


async def request_titles(sections, cache_key: str) -> Optional[list[str]]:
    """
    The one plan call: a title for each section, cached. Returns one title per
    section, or None when the call or its validation failed — the caller then
    names the sections itself. A short or overlong answer is padded or cut to
    the section count rather than thrown away.
    """
    cache = _load_plan_cache()
    cached = cache.get(cache_key)
    if isinstance(cached, list) and len(cached) == len(sections):
        return cached

    client = _llm_client()
    if client is None:
        return None
    try:
        raw = await asyncio.wait_for(
            evolution_data._call_with_retry(
                client, _TITLES_SYSTEM, build_titles_prompt(sections),
                json_mode=True, temperature=0.2, max_tokens=_TITLES_MAX_TOKENS,
            ),
            timeout=_PLAN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"Notes plan: titles call exceeded {_PLAN_TIMEOUT_SECONDS}s — using fallback titles.")
        return None
    if not raw:
        return None
    try:
        parsed = _TitlesResponse.model_validate_json(raw)
    except ValidationError as exc:
        print(f"Notes plan: unusable titles — {exc}")
        return None

    titles = [" ".join(t.split()[:_TITLE_MAX_WORDS]).strip() for t in parsed.titles]
    titles = (titles + [""] * len(sections))[:len(sections)]
    cache[cache_key] = titles
    _save_plan_cache(cache)
    return titles


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def group_by_document(concept_names, sources_by_concept, doc_order, positions, texts,
                      edges=(), encode=None) -> list[dict]:
    """
    The note's sections, decided without a model.

    Every concept belongs to the document where it is first taught
    (positions). Each document is cut into as many contiguous, equal ranges
    of its text as note_section_counts gives it (one for a short document),
    and each concept goes to the range its first position falls in. Ranges
    left empty are dropped and their neighbours widened over them, so a
    document's sections still tile it: each carries "range" (from, to), as
    fractions of the document's content, and must-cover picks are assigned
    by it.

    A concept whose name never appears in the text goes to the first of its
    own documents, into the section there holding most of its graph
    neighbours (`edges`), else the one whose concept names are closest to it
    (`encode`), else the document's last. Sections come out in document
    order, then source order, with no titles — request_titles names them.
    """
    rank = {doc: i for i, doc in enumerate(doc_order)}
    by_doc: dict[str, list[tuple[float, str]]] = defaultdict(list)
    unplaced: dict[str, list[str]] = defaultdict(list)
    for name in concept_names:
        if name in positions:
            d, frac = positions[name]
            by_doc[doc_order[d]].append((frac, name))
            continue
        own = sorted((s for s in sources_by_concept.get(name, ()) if s in rank), key=rank.get)
        if own:
            unplaced[own[0]].append(name)

    counts = note_section_counts({doc: len(texts.get(doc, "")) for doc in doc_order
                                  if by_doc.get(doc) or unplaced.get(doc)})
    sections = []
    for doc in doc_order:
        spots = sorted(by_doc.get(doc, ()))
        if not spots and not unplaced.get(doc):
            continue
        parts = counts.get(doc, 1)
        ranges: list[list[str]] = [[] for _ in range(parts)]
        for frac, name in spots:
            ranges[min(parts - 1, int(frac * parts))].append(name)
        kept = [k for k, names in enumerate(ranges) if names] or [parts - 1]
        # Widen the kept ranges over the dropped ones: each runs from its own
        # start (the first one from 0) to the next one's start (the last to 1).
        starts = [0.0] + [k / parts for k in kept[1:]]
        ends = starts[1:] + [1.0]
        here = [{"title": "", "concepts": list(ranges[k]), "document": doc, "range": (a, b)}
                for k, a, b in zip(kept, starts, ends)]
        for name in sorted(unplaced.get(doc, ())):
            _place_unplaced(name, here, edges, encode)
        sections += here
    return sections


def _place_unplaced(name, sections, edges, encode) -> None:
    """Adds a concept the text never names to the section, among `sections`
    (one document's), holding most of its graph neighbours; with no
    neighbours to go on, the one whose concept names are closest by
    embedding; with no encoder either, the last."""
    near = {e["to"] for e in edges if e.get("from") == name} | \
           {e["from"] for e in edges if e.get("to") == name}
    scores = [len(near.intersection(s["concepts"])) for s in sections]
    if max(scores, default=0) > 0:
        best = max(range(len(sections)), key=lambda i: (scores[i], -i))
    elif encode is not None and len(sections) > 1:
        vecs = encode([name] + [", ".join(s["concepts"]) or s["document"] for s in sections])
        best = max(range(len(sections)), key=lambda i: float(vecs[0] @ vecs[i + 1]))
    else:
        best = len(sections) - 1
    sections[best]["concepts"].append(name)


def fallback_title(section, index_in_doc: int, parts_in_doc: int) -> str:
    """A section's name when the titles call is unavailable: its document's
    own title, with the part number when the document was split."""
    from services.quiz_service import document_title
    base = document_title(section["document"]) if section.get("document") else "Mixed sources"
    return base if parts_in_doc == 1 else f"{base} (part {index_in_doc + 1})"


def name_sections(sections, titles: Optional[list[str]]) -> list[dict]:
    """Puts the titles on, falling back per section for any the call did not
    supply."""
    per_doc = Counter(s.get("document") for s in sections)
    seen: Counter = Counter()
    out = []
    for i, section in enumerate(sections):
        doc = section.get("document")
        title = (titles[i] if titles and i < len(titles) else "").strip()
        out.append({**section, "title": title or fallback_title(section, seen[doc], per_doc[doc])})
        seen[doc] += 1
    return out


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


# The single-word fallback in _occurrences is used only when that word is
# this rare in the document. "tree height" is not a phrase in Lecture 6, and
# its fallback "height" occurs ten times there, all through the AVL material,
# which pulled four AVL picks into the tree-height section; "rotation" (for
# "tree rotation") occurs twice and still places its concept.
_FALLBACK_MAX_HITS = 3


def _name_pattern(key: str) -> str:
    """A normalised name as a whole-word pattern that also takes a plural."""
    return rf"\b{re.escape(key)}(?:e?s)?\b"


# A soft hyphen is where a typesetter broke a word across lines; with what
# follows it (the line break) removed, "sub­\nstitution" is one word again.
_SOFT_HYPHEN_BREAK = re.compile("­\\s*")
_APOSTROPHES = re.compile("['‘’ʼ]")


def _prepare_text(text: str) -> str:
    """NFKC (folds the ﬁ and ﬀ ligatures the CS lecture PDFs carry) and
    soft-hyphen line breaks joined. Line structure is otherwise kept, since
    key_sentences.content_lines reads it."""
    return _SOFT_HYPHEN_BREAK.sub("", unicodedata.normalize("NFKC", str(text or "")))


def _match_form(text: str) -> str:
    """The form concept names and document text are compared in: prepared as
    above, apostrophes folded (fusion's canonical names drop them, so
    "laplaces demon" has to find "Laplace's demon"), then lowercased with
    separators and whitespace collapsed. Applied to both sides."""
    return _normalise_relation(_APOSTROPHES.sub("", _prepare_text(text)))


def _occurrences(name: str, text: str, others=()) -> list[int]:
    """
    Every place a concept appears in a document's normalised text, in order.

    The name is matched as whole words, allowing a plural. Longer concept
    names in `others` that contain it are masked first, so "binary search" is
    not found inside every "binary search tree" — that put Lecture 5's BST
    insertion pick in the binary search section.

    When the name itself never appears, its longest word (five letters or
    more) stands in, since extracted names are often a rewording ("data
    structure augmentation" for a lecture that says "augment") — but only
    when that word is rare in the document (_FALLBACK_MAX_HITS). A common
    word is not evidence of the concept.
    """
    key = _match_form(name)
    if not key:
        return []
    masked = text
    for other in sorted({_match_form(o) for o in others}, key=len, reverse=True):
        if other and other != key and re.search(rf"\b{re.escape(key)}\b", other):
            masked = re.sub(_name_pattern(other), lambda m: " " * len(m.group()), masked)
    hits = [m.start() for m in re.finditer(_name_pattern(key), masked)]
    if hits:
        return hits
    words = [w for w in re.split(r"[^a-z0-9]+", key) if len(w) >= 5]
    if not words or len(words) == len(key.split()) == 1:
        return []
    hits = [m.start() for m in re.finditer(_name_pattern(max(words, key=len)), masked)]
    return hits if len(hits) <= _FALLBACK_MAX_HITS else []


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
    return _match_form(key_sentences.content_text(_prepare_text(text)))


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
                      for pos in _occurrences(name, body, concept_names)]
        if spots:
            out[name] = sorted(spots)
    return out


def concept_positions(occurrences):
    """
    Where each concept first appears: {name: (document index, fraction)}, the
    earliest of its occurrences. group_by_document places each concept in
    its document, and its range of a split one, by this.
    """
    return {name: spots[0] for name, spots in occurrences.items()}


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

    A pick only goes to a section of its own document, whenever that
    document has any: a section is written from its own document's chunks,
    so a pick from another would be a sentence it cannot see. Lecture 6's
    "BSTs support insert, delete ... in O(h)" otherwise went to Lecture 5's
    BST section, whose names Lecture 6 had copied.

    When the sections carry text ranges (group_by_document), a pick whose
    position is found in the text goes to the section whose range contains
    it, and the concept-distance rule above is only the fallback for a pick
    that cannot be located. Distance let one concept named all through a
    document ("avl tree" in Lecture 6) take every pick, which left Lecture
    6's second range empty.
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
            key = _match_form(p["text"])[:40]
            at = body.find(key) if key else -1
            frac = at / max(len(body), 1) if at >= 0 else p["fraction"]
            flat.append((rank[doc], frac, p["text"], at >= 0))
    if not flat:
        return empty

    def work():
        return (encode([t for _, _, t, _ in flat]),
                encode([", ".join(s["concepts"]) for s in sections]))

    pick_emb, section_emb = await asyncio.to_thread(work)

    out: list[list] = [[] for _ in sections]
    for (doc, frac, text, located), vec in zip(flat, pick_emb):
        own = [i for i, s in enumerate(sections) if s.get("document") == doc_order[doc]]
        if located and own and all(sections[i].get("range") for i in own):
            inside = [i for i in own
                      if sections[i]["range"][0] <= frac < sections[i]["range"][1]
                      or (frac >= 1.0 and sections[i]["range"][1] >= 1.0)]
            if inside:
                out[inside[0]].append((doc, frac, text))
                continue
        pool = own or list(range(len(sections)))
        dist = {}
        for i in pool:
            near = [abs(f - frac) for c in sections[i]["concepts"]
                    for d, f in occurrences.get(c, ()) if d == doc]
            dist[i] = min(near) if near else float("inf")
        best = min(dist.values())
        tied = (pool if best == float("inf")
                else [i for i in pool if dist[i] <= best + _ASSIGN_TIE])
        choice = max(tied, key=lambda i: float(vec @ section_emb[i]))
        out[choice].append((doc, frac, text))
    return [[t for _, _, t in sorted(group)] for group in out]


# A section that ends up with this few must-cover picks after assignment has
# too little of its own to be a section, and merges into a neighbour.
MERGE_MAX_PICKS = 2


def _section_label(section) -> str:
    """How a section is named before it has a title (merging runs first):
    its title if set, else its document and first concepts."""
    if section.get("title"):
        return section["title"]
    head = ", ".join(section["concepts"][:3])
    return f"{section.get('document') or 'mixed'}: {head}"


def merge_thin_section(sections, must_cover, positions):
    """
    Folds the thinnest section into its nearest neighbour, at most once.

    A section qualifies with MERGE_MAX_PICKS or fewer must-cover picks. Its
    neighbour must be adjacent and in the same document: each section's
    document is where its earliest concept is taught. Of two such
    neighbours, the one whose own start is nearer wins. The neighbour keeps
    its title. Returns (sections, merged), `merged` naming what went where,
    or None when nothing qualified. The caller reassigns must-cover picks.
    """
    if len(sections) < 2:
        return sections, None

    def start(s):
        spots = [positions[c] for c in s["concepts"] if c in positions]
        return min(spots) if spots else None

    thin = [i for i, picks in enumerate(must_cover) if len(picks) <= MERGE_MAX_PICKS]
    for i in sorted(thin, key=lambda i: (len(must_cover[i]), i)):
        here = start(sections[i])
        if here is None:
            continue
        near = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(sections):
                there = start(sections[j])
                if there is not None and there[0] == here[0]:
                    near.append((abs(there[1] - here[1]), j))
        if not near:
            continue
        _, j = min(near)
        into = sections[j]
        concepts = (sections[i]["concepts"] + into["concepts"] if i < j
                    else into["concepts"] + sections[i]["concepts"])
        merged = {**into, "concepts": concepts}
        if sections[i].get("range") and into.get("range"):
            # Adjacent ranges of one document, so their union is contiguous.
            merged["range"] = (min(sections[i]["range"][0], into["range"][0]),
                               max(sections[i]["range"][1], into["range"][1]))
        out = [merged if k == j else s for k, s in enumerate(sections) if k != i]
        return out, {"merged": _section_label(sections[i]), "into": _section_label(into),
                     "picks": len(must_cover[i])}
    return sections, None


def section_document(concepts, positions, doc_order) -> Optional[str]:
    """
    The one document a section belongs to, or None when it spans several.

    Each concept counts for the document where it is first taught
    (positions). The document holding the most of them is the section's
    home; a tie between documents, or no placed concept at all, means the
    section spans several. With one section per document this is the
    document the section was planned from; only a merge across documents,
    which merge_thin_section does not make, would leave a tie.
    """
    counts = Counter(doc_order[positions[c][0]] for c in concepts
                     if c in positions and positions[c][0] < len(doc_order))
    if not counts:
        return None
    (top, n), *rest = counts.most_common()
    return None if rest and rest[0][1] == n else top


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
            "neighbours": section_neighbours(names, edges),
            # The document the section is written from. Its extracts, and so
            # its paragraph count, come from this document alone; None means
            # it spans several and draws on all of the note's documents.
            "document": section.get("document"),
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
# v13: the same call also writes a five-question section quiz. The request
# carries a distractor pool — concepts one link outside the section, contrasts
# marked — because concept-map context is what makes distractors plausible
# (Scaria et al. 2025). MUST COVER points are numbered so a question can say
# which one it tests. The quiz is checked in code and dropped, never retried.
#
# v14: the other sections' must-cover picks are listed as covered elsewhere,
# not only their concept names, and the paragraph count follows the source
# length up to 12 instead of stopping at 7. A section that belongs to one
# document is written from that document's chunks only, which also sets its
# paragraph count; the document is part of the cache key.
# v15: table cells must agree with the prose and the extracts for their row.
# A Lecture 5 section said the k-minute check is O(1) once the insertion point
# is found, and its table said O(n).
# v16: sections are smaller (about 3,500 characters) and picks stay in their
# own document, so what each section is asked to cover changed.
# v17: picks are assigned by text range and unplaced concepts by neighbours,
# so sections' concepts and must-cover lists changed again.
#
# Each bump changes the cache key, so bodies written under an older prompt are
# never served. Sections written before v13 therefore have no quiz until they
# are written again.
_WRITE_PROMPT_VERSION = "notes-write-v17"

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
    "OTHER SECTIONS and the points listed under KEY POINTS COVERED IN OTHER "
    "SECTIONS. The extracts may discuss them too, because they come from the "
    "same pages. Leave them out: do not explain, define, tabulate or give an "
    "example of them. Name one only where a sentence about this section's own "
    "concepts cannot be written without it.\n"
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
    "or more things along shared dimensions. Otherwise null. Every cell must "
    "agree with the extracts and with what your own prose says about that row: "
    "never give a different value — a cost, a complexity, a count — in the "
    "table than the body gives for the same thing. If the body says the "
    "k-minute check is O(1) once the insertion point is found, the table says "
    "O(1) too.\n"
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
    "produce an empty list or an example invented to have something there.\n"
    "\n"
    "QUIZ. Also fill quiz: five multiple-choice questions on this section, "
    "each answerable from what you wrote in lead and body alone.\n"
    "- bloom is the level: about two 'remember' (recall a fact or "
    "definition), two 'understand' (explain why or how, or tell two ideas "
    "apart) and one 'apply' (use an idea on a case the section describes).\n"
    "- d is the difficulty: 'easy', 'medium' or 'hard'.\n"
    "- One question per MUST COVER point where possible. Never two questions "
    "on the same fact.\n"
    "- ref says what the question tests: 'P1', 'P2', ... for a body "
    "paragraph, counted in the order you wrote them, or 'M1', 'M2', ... for "
    "a MUST COVER point as numbered in the request.\n"
    "- o: four options, three only if no fourth plausible one exists. "
    "Exactly one is correct; a is its 0-based index. Every option is "
    "different, and the correct answer never reappears reworded as a wrong "
    "one.\n"
    "- Wrong options are plausible confusions a student could actually make. "
    "Prefer concepts from the DISTRACTOR POOL, above all those marked as "
    "contrasting, or concepts of this section confused with each other. "
    "Never an option that is also true, never 'all of the above' or 'none "
    "of the above', never a joke.\n"
    # Added without bumping _WRITE_PROMPT_VERSION. The prompt text is not in
    # the cache key, and a bump would rewrite every cached section (one Gemini
    # call each, on a 20-a-day quota) for a cue that the serve-time shuffle
    # and the longest_correct count already cover. Cached sections keep the
    # quiz they were written with; sections written from now on get the line.
    "- Keep the options about the same length and grammatical form, so the "
    "longest or most detailed option is not a clue to the answer.\n"
    "- The question must not give its answer away in its wording.\n"
    "- why: ONE sentence saying why the answer is right, from the section.\n"
    "- The DISTRACTOR POOL is for wrong options only. It is not material: "
    "never explain or teach a pool concept in the notes."
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


class _WriteQuestion(BaseModel):
    """
    One section-quiz question, in quiz_service's question shape plus `bloom`
    and `ref`. Plain strings rather than enums: the values are checked by
    validate_quiz, which drops a bad question instead of failing the body.
    `id` is not asked for — it is derived from the text in code.
    """
    q: str
    o: list[str]
    a: int
    why: str
    d: str
    bloom: str
    ref: str


class _WriteBody(BaseModel):
    """The fixed block structure. Gemini takes this as a responseSchema, so the
    shape is enforced by the API rather than asked for in the prompt and
    validated afterwards."""
    lead: str
    body: list[_WriteParagraph]
    example: Optional[_WriteExample] = None
    table: Optional[_WriteTable] = None
    terms: Optional[list[_WriteTerm]] = None
    quiz: Optional[list[_WriteQuestion]] = None


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


def _write_key(section: dict, other_concepts=(), other_picks=()) -> str:
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
        "other_picks": list(other_picks),
        "must_cover": list(section.get("must_cover") or []),
        # Which document the extracts come from changes what is written.
        "document": section.get("document"),
        # The distractor pool is prompt input since v13, so it is identity.
        "neighbours": section.get("neighbours") or [],
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"{_WRITE_PROMPT_VERSION}|{GEMINI_MODEL}|section:{digest}"


async def gather_chunks(concept_names, documents=None) -> list[dict]:
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


# Body paragraphs per section follow the source: about one per
# PARAGRAPH_SOURCE_CHARS characters of the extracts the section is written
# from, and never more than PARAGRAPH_CEILING. The concept count sets the
# floor (about one paragraph per three concepts, at least two), unless the
# source is too short to fill it. A fixed maximum of seven squeezed a whole
# lecture into seven paragraphs once a section became a document; the length
# rule is what stopped a two-page handout from being padded to seven and
# repeating its equilibrium explanation three times.
MIN_PARAGRAPHS = 2
PARAGRAPH_CEILING = 12
PARAGRAPH_SOURCE_CHARS = 650


def paragraph_range(n_concepts: int, source_chars: Optional[int] = None) -> tuple[int, int]:
    """
    (fewest, most) body paragraphs for a section of `n_concepts` written from
    `source_chars` characters of extracts. Without a length, the concept
    count alone decides: one per three to one per two, within the ceiling.
    """
    by_concepts = max(MIN_PARAGRAPHS, -(-n_concepts // 3))
    if source_chars is None:
        least = min(PARAGRAPH_CEILING, by_concepts)
        return least, min(PARAGRAPH_CEILING, max(least, -(-n_concepts // 2)))
    by_length = max(MIN_PARAGRAPHS, source_chars // PARAGRAPH_SOURCE_CHARS)
    least = min(PARAGRAPH_CEILING, max(MIN_PARAGRAPHS, min(by_concepts, by_length)))
    most = max(least, min(PARAGRAPH_CEILING, by_length))
    return least, most


def build_write_prompt(section: dict, chunks: list[dict], other_concepts=(), other_picks=()) -> str:
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
    if other_picks:
        # The other sections' own key points, not just their concept names:
        # Lecture 6 is one chunk, so every section's extracts hold all of it,
        # and names alone left two sections both writing the AVL insert
        # example and the heap vs AVL table.
        lines += ["KEY POINTS COVERED IN OTHER SECTIONS — the extracts include "
                  "these, but other sections teach them; leave them out:"]
        lines += [f"- {p}" for p in other_picks]
        lines.append("")

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
            "SECTIONS. Quiz refs use these numbers.",
        ]
        lines += [f"M{i}. {p}" for i, p in enumerate(section["must_cover"], 1)]
        lines.append("")

    if section["relations"]:
        lines += [
            "CONNECTIONS TO COVER — a checklist, not material to quote. Each "
            "line is a link worth explaining; explain what it means in the "
            "words of the extracts above, and never use the label itself. "
            "[contrast] marks two ideas the source sets against each other:",
        ]
        lines += [f"- {e['from']} / {e.get('relation', 'relates to')} / {e['to']}"
                  + (" [contrast]" if is_contrast(e.get("relation")) else "")
                  for e in section["relations"]]
    else:
        lines.append("CONNECTIONS TO COVER: none recorded between these concepts.")

    pool = section.get("neighbours") or []
    lines.append("")
    if pool:
        lines += [
            "DISTRACTOR POOL — concepts one link outside this section. Use them "
            "only as wrong quiz options; never explain them in the notes:",
        ]
        for n in pool:
            if n.get("contrasts"):
                lines.append(f"- {n['name']} — contrasts with {', '.join(n['contrasts'])}")
            else:
                lines.append(f"- {n['name']} — linked to {', '.join(n['links'])}")
    else:
        lines.append("DISTRACTOR POOL: none — draw wrong options from this "
                     "section's own concepts.")
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


# Section quiz. Five asked for; fewer than QUIZ_MIN_QUESTIONS surviving the
# checks is not worth offering, and the section is saved without one.
QUIZ_QUESTIONS = 5
QUIZ_MIN_QUESTIONS = 3
_QUIZ_DIFFICULTIES = ("easy", "medium", "hard")
_QUIZ_BLOOM = {"remember": "easy", "understand": "medium", "apply": "hard"}
_QUIZ_REF = re.compile(r"^\s*([PM])\s*(\d+)\s*$", re.I)


def _quiz_ref(raw, paragraphs: int, must_cover: int) -> Optional[dict]:
    """'P2' or 'M1' as {"kind", "index"} (0-based), or None when it does not
    point at anything that exists. A bad ref costs the question its Review
    link, not the question."""
    match = _QUIZ_REF.match(str(raw or ""))
    if not match:
        return None
    kind = "paragraph" if match.group(1).upper() == "P" else "must_cover"
    index = int(match.group(2)) - 1
    limit = paragraphs if kind == "paragraph" else must_cover
    return {"kind": kind, "index": index} if 0 <= index < limit else None


# Item-writing flaws. The first four drop a question; the last three are
# counted only, because on the saved quizzes they fired mostly on deliberate
# contrasts (O(1) / O(n) / O(lg n)) and on correct options that happen to say
# "all".
IWF_DROP = ("duplicate_stem", "all_none_of_the_above", "true_false", "answer_in_stem")
IWF_LOG = ("longest_correct", "absolute_terms", "duplicate_options")
_STEM_JACCARD = 0.7
_OPTION_JACCARD = 0.8
_ABOVE = re.compile(r"\b(?:all|none|both|neither) of (?:the )?(?:above|these|the options)\b", re.I)
_ABSOLUTE = re.compile(r"\b(?:always|never|all|none|only|every|nothing|completely|entirely)\b", re.I)
_TRUE_FALSE = frozenset({"true", "false", "yes", "no", "correct", "incorrect"})
_STOPWORDS = frozenset(
    "a an the of in on at to for from by with and or is are was were be been being which what "
    "who whom whose when where why how does do did this that these those it its as into than "
    "then there their they can could would should will may might must".split())


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _name_key(text))
    content = {w for w in words if w not in _STOPWORDS}
    return content or set(words)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _flaws(q: str, options: list[str], answer: int, kept_stems: list[set]) -> list[str]:
    """The item-writing flaws one question shows, by the names in IWF_DROP
    and IWF_LOG."""
    found = []
    stem = _content_words(q)
    if any(_jaccard(stem, other) >= _STEM_JACCARD for other in kept_stems):
        found.append("duplicate_stem")
    if any(_ABOVE.search(o) for o in options):
        found.append("all_none_of_the_above")
    if {_name_key(o).rstrip(".!?;: ") for o in options} <= _TRUE_FALSE:
        found.append("true_false")
    correct = _name_key(options[answer]).rstrip(".!?;: ")
    if len(correct) >= 3 and re.search(rf"(?<!\w){re.escape(correct)}(?!\w)", _name_key(q)):
        found.append("answer_in_stem")
    lengths = [len(o) for o in options]
    others = [n for i, n in enumerate(lengths) if i != answer]
    if (lengths.count(max(lengths)) == 1 and lengths[answer] == max(lengths)
            and lengths[answer] >= 1.25 * (sum(others) / len(others))):
        found.append("longest_correct")
    if any(_ABSOLUTE.search(o) for o in options):
        found.append("absolute_terms")
    words = [_content_words(o) for o in options]
    if any(_jaccard(words[i], words[j]) >= _OPTION_JACCARD
           for i in range(len(words)) for j in range(i + 1, len(words))):
        found.append("duplicate_options")
    return found


def validate_quiz(items, quiz_id: str, paragraphs: int,
                  must_cover: int) -> tuple[Optional[list], int, dict]:
    """
    The section quiz, checked in code — no second model reads it.

    A question is dropped when it has no question or explanation text, fewer
    than three or more than four options, an empty option, two options that
    match once normalised (which also catches the answer repeated as a wrong
    option), or an answer index out of range; or when it shows one of the
    IWF_DROP flaws, a stem much like one already kept among them. IWF_LOG
    flaws are counted and the question kept. Returns (questions, dropped,
    flags), flags counting every flaw seen by name; questions is None when
    fewer than QUIZ_MIN_QUESTIONS survive.
    """
    kept, stems, dropped = [], [], 0
    flags = {name: 0 for name in IWF_DROP + IWF_LOG}
    for item in items or []:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            dropped += 1
            continue
        q = str(item.get("q") or "").strip()
        why = str(item.get("why") or "").strip()
        options = [str(o or "").strip() for o in (item.get("o") or [])]
        answer = item.get("a")
        # Trailing sentence punctuation is dropped for the comparison, so "the
        # minimum." repeats "The minimum". Symbols stay: C++ is not C.
        keys = [_name_key(o).rstrip(".!?;: ") for o in options]
        if (not q or not why
                or not 3 <= len(options) <= 4
                or not all(keys)
                or len(set(keys)) != len(keys)
                or not isinstance(answer, int) or isinstance(answer, bool)
                or not 0 <= answer < len(options)):
            dropped += 1
            continue
        found = _flaws(q, options, answer, stems)
        for name in found:
            flags[name] += 1
        if any(name in IWF_DROP for name in found):
            dropped += 1
            continue
        stems.append(_content_words(q))
        bloom = str(item.get("bloom") or "").strip().lower()
        bloom = bloom if bloom in _QUIZ_BLOOM else None
        d = str(item.get("d") or "").strip().lower()
        if d not in _QUIZ_DIFFICULTIES:
            d = _QUIZ_BLOOM.get(bloom, "medium")
        digest = hashlib.sha1(f"{quiz_id}:{q}".encode("utf-8")).hexdigest()[:10]
        kept.append({
            "id": f"{quiz_id}-{digest}", "q": q, "o": options, "a": answer,
            "why": why, "d": d, "bloom": bloom,
            "ref": _quiz_ref(item.get("ref"), paragraphs, must_cover),
        })
    dropped += max(0, len(kept) - QUIZ_QUESTIONS)
    kept = kept[:QUIZ_QUESTIONS]
    return (kept if len(kept) >= QUIZ_MIN_QUESTIONS else None), dropped, flags


def section_quiz_id(cache_key: str) -> str:
    """A section quiz's id, from its write-cache key. Section ids restart with
    the process; the cache key does not, so results stored against this id
    still find their quiz after a restart."""
    return "sec-" + hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:12]


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


async def write_body(section: dict, documents=None, other_concepts=(),
                     other_picks=()) -> tuple[Optional[dict], Optional[float]]:
    """
    Writes one section's prose. Consults the write cache first and writes back
    on success.

    `documents` are the note's files, which extracts are limited to;
    `other_concepts` are what the note's other sections cover, and
    `other_picks` their must-cover sentences.

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
    cache_key = _write_key(section, other_concepts, other_picks)
    cached = _load_write_cache().get(cache_key)
    if isinstance(cached, dict) and cached.get("lead"):
        return cached, None

    client = _gemini_client()
    if client is None:
        return None, None

    chunks = await gather_chunks(section["concepts"], documents)
    if not chunks:
        print(f"Notes write: no source text retrieved for section {section['id']} "
              f"({len(section['concepts'])} concepts) — nothing to write from.")
        return None, None

    prompt = build_write_prompt(section, chunks, other_concepts, other_picks)
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

    # Checked, never retried: a section with a weak quiz is saved without one
    # rather than costing another call or failing the write.
    quiz_id = section_quiz_id(cache_key)
    quiz, dropped, flags = validate_quiz(body.get("quiz"), quiz_id, len(body.get("body") or []),
                                         len(section.get("must_cover") or []))
    body["quiz"] = quiz
    body["quiz_id"] = quiz_id if quiz else None
    body["quiz_dropped"] = dropped
    body["quiz_flags"] = flags
    logger.info("notes_write_quiz section=%s kept=%d dropped=%d flags=%s",
                section["id"], len(quiz or []), dropped,
                {name: n for name, n in flags.items() if n})

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
            # A malformed quiz must not cost the section: try again without it.
            try:
                raw = json.loads(response.text or "")
                raw.pop("quiz", None)
                parsed = _WriteBody.model_validate(raw)
                print(f"Notes write: quiz unusable, section kept without it — {exc}")
            except (ValidationError, ValueError, TypeError, AttributeError):
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
    Plans a note: one section per document, or contiguous ranges of a long
    one, each holding the concepts first taught there. Must-cover picks are
    assigned, a thin section is merged, and one model call titles the rest.

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
    # Diagnostics only: grouping no longer uses the relations, but a corpus
    # whose labels this classifier cannot read is still worth reporting.
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
    logger.info("notes_plan concepts=%d sections_by_length=%s",
                len(concept_names), source_cap if source_cap is not None else "unknown")

    doc_order = _document_order(user["id"], body.document_ids)
    texts = document_texts(filenames)
    occurrences = concept_occurrences(concept_names, sources_by_concept, doc_order, texts)
    positions = concept_positions(occurrences)

    sections = group_by_document(concept_names, sources_by_concept, doc_order, positions, texts,
                                 edges=edges, encode=_encoder())
    if not sections:
        raise HTTPException(
            status_code=400,
            detail="None of those documents' concepts could be placed in their text.",
        )

    must_cover = await assign_must_cover(sections, doc_order, occurrences, texts)
    sections, merged = merge_thin_section(sections, must_cover, positions)
    if merged:
        logger.info("notes_plan_merge merged=%r into=%r picks=%d",
                    merged["merged"], merged["into"], merged["picks"])
        must_cover = await assign_must_cover(sections, doc_order, occurrences, texts)

    # The one model call: titles for sections that are already decided. It
    # can fail without failing the plan — the sections are named from their
    # documents instead.
    titles_key = _call_key(_plan_key(_PLAN_PROMPT_VERSION, body.document_ids, body.days),
                           "titles", ["\x01".join(s["concepts"]) for s in sections])
    titles = await request_titles(sections, titles_key)
    if titles is None:
        print("Notes plan: titles call unavailable — naming sections after their documents.")
    sections = name_sections(sections, titles)

    for section, picks in zip(sections, must_cover):
        section["must_cover"] = picks
        # Grouping already fixed each section's document; a merge keeps the
        # neighbour's. section_document is the check for anything spanning two.
        section["document"] = (section.get("document")
                               or section_document(section["concepts"], positions, doc_order))
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
        "plan": {"grouping": "per document", "sections": len(sections),
                 "titles": "model" if titles is not None else "fallback",
                 "merged": merged},
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


def section_sources(note: dict, section: dict) -> set[str]:
    """The documents a section's extracts are retrieved from: its own
    document when it has one, else all of the note's. Cross-document links
    still reach it through its relations, shown under Connections."""
    home = section.get("document")
    return {home} if home in note["documents"] else set(note["documents"])


def _register_quiz(user_id, note: dict, section: dict) -> None:
    """Records a written section's quiz with its note and section titles, so
    the Quiz tab can list it after the in-memory note is gone."""
    body = section.get("body") or {}
    if not body.get("quiz") or not body.get("quiz_id"):
        return
    from services.quiz_service import register_section_quiz, shuffle_quiz
    # Stored in the order the section serves it, so the Quiz tab and the
    # Notes pane show the same options in the same places.
    register_section_quiz(user_id, body["quiz_id"], note["title"], section["title"],
                          section["index"], shuffle_quiz(body["quiz"]))


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
        _register_quiz(user["id"], note, section)
        return {"section": _section_public(section, _passed_quizzes(user["id"])),
                "note_id": note["id"], "generated": False}

    if _gemini_client() is None:
        raise HTTPException(
            status_code=503,
            detail="Note writing is not configured. Set GEMINI_API_KEY in backend/.env.",
        )

    task = _WRITES_IN_FLIGHT.get(section_id)
    if task is None:
        others = [c for s in note["sections"] if s is not section for c in s["concepts"]]
        other_picks = [p for s in note["sections"] if s is not section
                       for p in s.get("must_cover") or []]
        task = asyncio.create_task(
            write_body(section, section_sources(note, section), others, other_picks))
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
    _register_quiz(user["id"], note, section)
    return {"section": _section_public(section, _passed_quizzes(user["id"])),
            "note_id": note["id"], "generated": True}


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
