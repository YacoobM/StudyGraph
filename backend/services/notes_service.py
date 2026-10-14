"""Revision notes: plans a note's sections from the concept graph and writes each section with
Gemini when opened. Notes live in memory, like the engine they point at."""

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

from app.config import OUTPUT_DIR
from services import evolution_data, key_sentences
from services.note_days import split_across_days  # noqa: F401  (used by build_note)

router = APIRouter(tags=["notes"])
logger = logging.getLogger(__name__)

# Documents are cut into sections of about SECTION_CHARS; one under SINGLE_SECTION_BELOW stays
# whole. Past the caps, sections get longer, not more numerous.
SECTION_CHARS = 3500
SINGLE_SECTION_BELOW = 4500
MAX_SECTIONS_PER_DOCUMENT = 6
MAX_NOTE_SECTIONS = 10


def document_section_count(chars: int, size: float = SECTION_CHARS) -> int:
    """Sections one document is worth at `size` characters each: one if short, else about chars
    / size, between 2 and MAX_SECTIONS_PER_DOCUMENT."""
    if chars < SINGLE_SECTION_BELOW * size / SECTION_CHARS:
        return 1
    return min(MAX_SECTIONS_PER_DOCUMENT, max(2, round(chars / size)))


def note_section_counts(lengths: dict[str, int]) -> dict[str, int]:
    """{document: section count} for one note. Over MAX_NOTE_SECTIONS the section size grows for
    every document alike."""
    size = float(SECTION_CHARS)
    counts = {d: document_section_count(n, size) for d, n in lengths.items()}
    while sum(counts.values()) > MAX_NOTE_SECTIONS and any(c > 1 for c in counts.values()):
        size *= 1.05
        counts = {d: document_section_count(n, size) for d, n in lengths.items()}
    return counts


# Sections are grouped in code; the one plan call only titles them, so it gets a small budget.
_TITLE_MAX_WORDS = 8
_TITLES_MAX_TOKENS = 400

# --- write step ---

# Runs on Gemini when a section is opened: its input is thousands of tokens of source text.
_CHUNKS_PER_CONCEPT = 3        # retrieve_chunks' own default
# retrieve_chunks has no source filter, so each concept asks for this many hits and keeps the first
# few from the note's own documents.
_RETRIEVE_POOL = 40
_MAX_WRITE_CHUNKS = 30         # bound on a degraded section holding many concepts
_WRITE_TIMEOUT_SECONDS = 120   # a lone section; groups get longer (_write_timeout)
# Extra time per section past the first in a grouped call: 300 s for four.
_WRITE_TIMEOUT_PER_EXTRA_SECTION = 60

# Several sections per call: the 20-a-day request quota runs out first. Measured on 9 sections:
# output at most 3,763 tokens, thinking mean 3,300 (cap is 1.5x).
_GROUP_MAX_SECTIONS = 4
_GROUP_OUTPUT_BUDGET = 40_000
_OUTPUT_PER_SECTION = 4_000
_THINKING_PER_SECTION = 5_000
_THINKING_BUDGET_MAX = 24_576

# Gemini answers a busy model with 503. One retry only: every attempt, refused ones included,
# counts against the 20-a-day quota.
_WRITE_MAX_ATTEMPTS = 2
_WRITE_RETRY_BASE_SECONDS = 2

# Total wait between attempts: a reader is waiting, and each extra attempt spends the quota it
# waits for.
_WRITE_RETRY_BUDGET_SECONDS = 30

# Ceiling on the titles call, retries included: a reader is waiting, so a throttled key must fail
# fast.
_PLAN_TIMEOUT_SECONDS = 60


# --- graph source ---

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
    """(concepts, edges, canonical_nodes), or empty values. Prefers fused_edges: fusion resolves
    duplicate names and edge direction."""
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


# --- store: swap these four for SQLite and nothing above them changes ---

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
    """Done when marked so or its quiz has a passing attempt; the latter survives reloads and
    restarts."""
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
    """The body as served: quiz shuffled, pass mark attached. The stored body keeps the model's
    order."""
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
        # Must-cover source sentences; the UI no longer reads them, kept as data.
        "must_cover": section.get("must_cover") or [],
        "body": _served_body(section["body"]),
        "done": _is_done(section, passed),
    }


# --- relation classifier ---

# Fusion orients taxonomic edges, so direction carries the broader signal. Labels are an open
# vocabulary, so they are matched tolerantly.

_WS = re.compile(r"\s+")
_SEPARATORS = re.compile(r"[_\-‐‑‒–—−]+")


def _normalise_relation(rel: str) -> str:
    """Lowercase, separators to spaces, collapse whitespace."""
    text = _SEPARATORS.sub(" ", str(rel or "").lower())
    return _WS.sub(" ", text).strip()


# The quote characters graph_fusion._STRIP_CHARS removes; brackets and & + . / stay, so C++, .NET
# and O(n) survive.
_STRIP_CHARS = re.compile(r"[,;:\"'`‘’“”]")


def _fold_diacritics(text: str) -> str:
    """Strip combining marks, so schrödinger matches Schrodinger. NFD rather
    than NFKD: the compatibility forms would also rewrite ﬁ and ①."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)
    )


def _name_key(name: str) -> str:
    """Matching form of a concept name for reading the model's answer back: NFC, quotes removed,
    accents folded, separators normalised."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = _fold_diacritics(_STRIP_CHARS.sub("", text).lower())
    return _WS.sub(" ", _SEPARATORS.sub(" ", text)).strip()


# Needles match whole words; a trailing '*' takes any suffix ('causes' must not match 'use').
# "A <label> B" where A is the broader concept, so A comes first.
_BROADER_FIRST = ("generalis*", "generaliz*", "subsum*", "encompass*",
                  "underli*", "include*", "contain*", "comprise*",
                  "consists of", "consist of", "has part", "cover*")
# "A <label> B" where B is the broader concept: broader first means B before A.
_NARROWER_FIRST = ("is a", "is an", "type of", "is type", "has type", "kind of",
                   "form of", "subtype*", "special case", "instance of",
                   "specialis*", "specializ*", "part of", "belongs to",
                   "belong to", "member of", "component of", "subset of",
                   "example of")

# A bare copula ('determinism is philosophy') is taxonomic. Matched on the whole label: 'is' alone
# would also catch 'is opposite of'.
_COPULA = frozenset({"is", "are", "was", "were"})

# "A <label> B" where A must be understood first.
_FORWARD = ("prerequisite*", "precede*", "comes before", "before", "enable*",
            "allow*", "leads to", "lead to", "cause*", "results in",
            "result in", "produce*", "yield*", "motivate*", "introduce*",
            "define*", "establish*", "applied to", "apply to")
# "A <label> B" where B must be understood first: a dependency, read backwards.
_REVERSE = ("require*", "depends on", "depend on", "relies on", "rely on",
            "need*", "presuppose*", "assume*", "builds on", "build on",
            "extend*", "based on", "derived from", "use*", "operates on",
            "operate on", "follows from")

# Symmetric or information-free labels: they group concepts without ordering them.
# explain* is directional but not a teaching order (a theorem explains a cut taught before it).
_SYMMETRIC = ("relate*", "associated with", "connected to", "similar to",
              "same as", "equivalent to", "compar*", "contrast*", "differ*",
              "distinguish*", "alternative to", "complement*", "explain*")


_INVERSE = {"forward": "reverse", "reverse": "forward", "cohesion": "cohesion"}


def _match(label: str, needles: tuple[str, ...]) -> bool:
    """Whole-word phrase match; a trailing '*' covers a verb's inflections without matching
    inside another word."""
    for needle in needles:
        if needle.endswith("*"):
            pattern = rf"\b{re.escape(needle[:-1])}"
        else:
            pattern = rf"\b{re.escape(needle)}\b"
        if re.search(pattern, label):
            return True
    return False


def _classify(relation: str) -> tuple[str, bool]:
    """(kind, recognised). recognised tells a placed label from a defaulted one, so a corpus of
    unknown verbs can be spotted."""
    label = _normalise_relation(relation)
    if not label:
        return "cohesion", False

    # Passive forms are the active verb with ends swapped ('A used by B' is 'B uses A'); checked
    # first, so 'used by' is not read as 'use'.
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
    """'forward' (teaching order follows the edge), 'reverse' (runs against it) or 'cohesion'
    (no order). Unsure labels fall to cohesion."""
    return _classify(relation)[0]


def unrecognised_labels(edges: list[dict]) -> dict[str, int]:
    """Labels the vocabulary did not match, with counts; a large count means the plan has little
    order."""
    counts: Counter = Counter()
    for e in edges:
        if not _classify(e.get("relation"))[1]:
            counts[_normalise_relation(e.get("relation")) or "(blank)"] += 1
    return dict(counts.most_common())


def derive_constraints(edges: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """Splits edges into ordering constraints [(before, after)] and cohesion hints [(a, b)]; a
    pair ordered both ways becomes a hint."""
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


# --- graph filtering ---

def filter_graph_to_documents(filenames: set[str]):
    """Concepts by source document and the edges whose ends both survive. Names go through
    canonical_nodes, which fused edges already use."""
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
        # The extractor's sentence about the link, shown under Connections instead of the label.
        kept_edges.append({"from": a, "relation": relation, "to": b,
                           "summary": str(e.get("summary") or "").strip()})

    return names_by_source, sources_by_concept, kept_edges


# Labels that set two ideas against each other. A subset of _SYMMETRIC: these
# are the pairs a student confuses, so they make the best wrong options.
_CONTRAST = ("contrast*", "differ*", "distinguish*", "unlike", "versus", "vs",
             "opposite*", "opposed to", "as opposed to", "alternative to")


def is_contrast(relation: str) -> bool:
    return _match(_normalise_relation(relation), _CONTRAST)


# Distractor pool size: choice for five questions' wrong options without outweighing the section.
_MAX_DISTRACTOR_POOL = 12


def section_neighbours(names, edges: list[dict]) -> list[dict]:
    """Concepts one link outside a section, the distractor pool for its quiz: contrasts first,
    then the most connected."""
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


# --- prompt ---

# Bump when the plan's output changes; it is part of the plan cache key.
_PLAN_PROMPT_VERSION = "notes-plan-v10"

def document_texts(filenames: set[str]) -> dict[str, str]:
    """Each document's text rebuilt from the engine's chunks; chunk i starts at i * (size -
    overlap)."""
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
    """Sections the documents' text is worth in all (note_section_counts), or None without
    chunks."""
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


# --- response validation ---

class _TitlesResponse(BaseModel):
    titles: list[str] = Field(min_length=1)


# --- plan cache: keyed on prompt version and model; notes themselves live in _NOTES ---

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
    """Writes a cache, best-effort, via a temporary file swapped in so readers never see half a
    file."""
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


# Serialises write-cache updates: concurrent writes each saving the dict they loaded used to drop
# each other's entries.
_WRITE_CACHE_LOCK = asyncio.Lock()


async def _store_written(cache_key: str, body: dict) -> None:
    """Adds one body to the write cache, re-reading the file first so entries
    saved by other sections since this one started are kept."""
    async with _WRITE_CACHE_LOCK:
        cache = _load_write_cache()
        cache[cache_key] = body
        _save_write_cache(cache)


def _plan_key(version: str, document_ids: list[int], days: Optional[int]) -> str:
    """Cache key for one plan; includes the day count so an entry matches exactly one returned
    plan."""
    ids = ",".join(str(i) for i in sorted(document_ids))
    return f"{version}|{evolution_data.LLM_MODEL}|{ids}|days={days or 0}"


def _call_key(prefix: str, kind: str, concept_names) -> str:
    """Cache key for one call inside a plan: the plan key plus a hash of the sorted concepts
    asked about."""
    digest = hashlib.sha1(
        "\x00".join(sorted(concept_names)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}|{kind}:{digest}"


# --- LLM ---

_client = None


def _low_reasoning(client):
    """Wraps the client to log usage and default reasoning_effort to 'low' (at default effort the
    model spent all of max_tokens thinking)."""
    create = client.chat.completions.create

    def create_low(**kwargs):
        kwargs.setdefault("reasoning_effort", "low")
        response = create(**kwargs)
        # Actual usage, for re-checking the max_tokens headroom when the model changes.
        usage = getattr(response, "usage", None)
        finish = response.choices[0].finish_reason if response.choices else None
        logger.debug(
            "notes_plan_usage model=%s reasoning_effort=%s completion_tokens=%s "
            "prompt_tokens=%s max_tokens=%s finish_reason=%s",
            kwargs.get("model"), kwargs.get("reasoning_effort"),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "prompt_tokens", None),
            kwargs.get("max_completion_tokens", kwargs.get("max_tokens")), finish,
        )
        return response

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_low))
    )


def _llm_client():
    """The provider's client for the titles call, built lazily so importing needs no key. Retries
    and pacing belong to evolution_data._call_with_retry."""
    global _client
    if _client is None:
        from app.config import OPENAI_API_KEY
        from services.llm_provider import create_llm_client
        if not OPENAI_API_KEY:
            return None
        # max_retries=1 leaves retrying to _call_with_retry rather than doubling
        # it up at the SDK layer.
        _client = _low_reasoning(create_llm_client(OPENAI_API_KEY, max_retries=1))
    return _client


async def request_titles(sections, cache_key: str) -> Optional[list[str]]:
    """The one plan call: a title per section, cached. None when the call fails; a short or long
    answer is padded or cut."""
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
                json_mode=True, max_tokens=_TITLES_MAX_TOKENS,
            ),
            timeout=_PLAN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"Notes plan: titles call exceeded {_PLAN_TIMEOUT_SECONDS}s — using fallback titles.")
        return None
    except Exception as exc:
        # _call_with_retry raises once its retries are spent; the plan still stands without titles.
        print(f"Notes plan: titles call failed ({type(exc).__name__}: {exc}) — using fallback titles.")
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


# --- assembly ---

def group_by_document(concept_names, sources_by_concept, doc_order, positions, texts,
                      edges=(), encode=None) -> list[dict]:
    """The note's sections, decided without a model: each concept goes to the text range of its
    first-taught document; unplaced ones join their neighbours' section."""
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
    """Adds a concept the text never names to the section holding most of its neighbours, else
    the closest by embedding, else the last."""
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


_NATURAL = re.compile(r"(\d+)")


def _natural_key(text: str):
    """Sort key that reads digit runs as numbers: Lecture 5 < Lecture 10."""
    return [int(p) if p.isdigit() else p.lower() for p in _NATURAL.split(text or "")]


def _week_number(week) -> float:
    """'Week 3', '3' or 3 to 3.0; anything else sorts after every numbered week."""
    match = re.search(r"\d+", str(week or ""))
    return float(match.group()) if match else float("inf")


# A name's single-word fallback is used only when that word is this rare; a common word is no
# evidence of the concept.
_FALLBACK_MAX_HITS = 3


def _name_pattern(key: str) -> str:
    """A normalised name as a whole-word pattern that also takes a plural."""
    return rf"\b{re.escape(key)}(?:e?s)?\b"


# A soft hyphen is where a typesetter broke a word across lines; with what
# follows it (the line break) removed, "sub­\nstitution" is one word again.
_SOFT_HYPHEN_BREAK = re.compile("­\\s*")
_APOSTROPHES = re.compile("['‘’ʼ]")


def _prepare_text(text: str) -> str:
    """NFKC (folds PDF ligatures) with soft-hyphen line breaks joined; lines are kept for
    key_sentences."""
    return _SOFT_HYPHEN_BREAK.sub("", unicodedata.normalize("NFKC", str(text or "")))


def _match_form(text: str) -> str:
    """The form names and text are compared in, on both sides: prepared, apostrophes folded
    (canonical names drop them), lowercased, separators collapsed."""
    return _normalise_relation(_APOSTROPHES.sub("", _prepare_text(text)))


def _occurrences(name: str, text: str, others=()) -> list[int]:
    """Every place a concept appears in normalised text: whole words, plural allowed, longer
    names containing it masked. Falls back to its longest word if rare."""
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


def _content_body(text: str) -> str:
    """A document's content normalised for matching, without running heads, front matter,
    overview or references (key_sentences.content_lines)."""
    return _match_form(key_sentences.content_text(_prepare_text(text)))


def concept_occurrences(concept_names, sources_by_concept, doc_order, texts):
    """{name: [(document index, fraction through its content), ...]}, sorted; the fractions line
    up with the must-cover picks."""
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
    """Each concept's first appearance, {name: (document index, fraction)}; grouping places
    concepts by it."""
    return {name: spots[0] for name, spots in occurrences.items()}


# --- must-cover sentences: picked by key_sentences, assigned at plan time, checked after writing ---

# Sections whose nearest concept is within this fraction of the closest are tied; similarity to
# their concept names breaks the tie.
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
    """Each section's must-cover sentences in source order. A pick goes to the section whose
    text range holds it, else to the nearest concept in its own document."""
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
    """Folds the thinnest section (MERGE_MAX_PICKS or fewer picks) into an adjacent one of the
    same document, at most once. Returns (sections, merged)."""
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
    """The one document a section belongs to (where most of its concepts are first taught), or
    None on a tie."""
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
    """Each must-cover pick's best cosine against the written sentences, banded covered, weak or
    missing. Recorded only."""
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


def build_note(user_id, title, document_ids, filenames, sections,
               sources_by_concept, edges, days):
    """Assembles the note and its sections; bodies stay empty until written."""
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
            # Recorded now: by write time the engine may hold a different graph, or none.
            "concepts": names,
            "relations": [
                e for e in edges if e["from"] in inside and e["to"] in inside
            ],
            "sources": sorted(
                {s for n in names for s in sources_by_concept.get(n, ())}
            ),
            "must_cover": list(section.get("must_cover") or []),
            "neighbours": section_neighbours(names, edges),
            # The section's source document, which alone supplies its extracts; None means it spans
            # several.
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


def _title_for(filenames: list[str]) -> str:
    if len(filenames) == 1:
        return Path(filenames[0]).stem
    return "Revision notes"


# --- write step: a planned section into prose, when a reader opens it ---

# Bump when the prompt or a section's inputs change; it is in the cache key, so older bodies are
# never served.
_WRITE_PROMPT_VERSION = "notes-write-v18"

# Pinned, not a '-latest' alias: the alias returned 503 while this model answered, and a moving
# alias makes cached bodies irreproducible.
GEMINI_MODEL = "gemini-3.5-flash-lite"

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
    # Added without a version bump: a bump rewrites every cached section, one quota request each,
    # for a cue the shuffle already covers.
    "- Keep the options about the same length and grammatical form, so the "
    "longest or most detailed option is not a clue to the answer.\n"
    "- The question must not give its answer away in its wording.\n"
    "- why: ONE sentence saying why the answer is right, from the section.\n"
    "- The DISTRACTOR POOL is for wrong options only. It is not material: "
    "never explain or teach a pool concept in the notes."
)

# Appended to _WRITE_SYSTEM for a call that writes several sections.
_GROUP_SYSTEM = (
    "\n\n"
    "SEVERAL SECTIONS IN ONE REQUEST. This request holds more than one section "
    "of the note, each between '=== SECTION <id> ===' and '=== END SECTION "
    "<id> ==='. Write every one by the rules above as if it were the only one, "
    "from its own block alone: its own extracts, concepts, paragraph count, "
    "must-cover points, connections and distractor pool. The extracts of "
    "different sections may overlap; each section still covers only its own "
    "concepts and MUST COVER points.\n"
    "- The other sections in this request are among each section's OTHER "
    "SECTIONS. Never explain, define, tabulate or give an example in one "
    "section of what another section in the request covers, and never repeat "
    "a paragraph, example, table, glossary term or quiz question between them.\n"
    "- Quiz refs count within each section: P1 is that section's first "
    "paragraph, M1 its first MUST COVER point.\n"
    "- Return one entry in sections for every section, with id set to the "
    "section's id exactly as given."
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
    """A run of paragraph text and its marks; bold and key are required so the model decides for
    every span."""
    text: str
    bold: bool
    key: bool


class _WriteParagraph(BaseModel):
    heading: str
    parts: list[_WriteSpan]


class _WriteQuestion(BaseModel):
    """One section-quiz question plus bloom and ref. Plain strings: validate_quiz drops a bad
    question instead of failing the body."""
    q: str
    o: list[str]
    a: int
    why: str
    d: str
    bloom: str
    ref: str


class _WriteBody(BaseModel):
    """The fixed block structure, enforced by Gemini as a responseSchema."""
    lead: str
    body: list[_WriteParagraph]
    example: Optional[_WriteExample] = None
    table: Optional[_WriteTable] = None
    terms: Optional[list[_WriteTerm]] = None
    quiz: Optional[list[_WriteQuestion]] = None


class _WriteGroupSection(BaseModel):
    """One section of a grouped answer: its id as given in the request, then
    the _WriteBody blocks."""
    id: str
    lead: str
    body: list[_WriteParagraph]
    example: Optional[_WriteExample] = None
    table: Optional[_WriteTable] = None
    terms: Optional[list[_WriteTerm]] = None
    quiz: Optional[list[_WriteQuestion]] = None


class _WriteGroup(BaseModel):
    sections: list[_WriteGroupSection]


_gemini_client_instance = None


def _gemini_client():
    """Built on first use, since a client without a key raises. None when neither GEMINI_API_KEY
    nor GEMINI_VISION_API_KEY is set."""
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
    """Cache key for a section's prose: prompt version, model and the section's plan-time
    identity. Extracts are left out, as in the plan cache."""
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
        # The distractor pool is prompt input, so it is identity.
        "neighbours": section.get("neighbours") or [],
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"{_WRITE_PROMPT_VERSION}|{GEMINI_MODEL}|section:{digest}"


async def gather_chunks(concept_names, documents=None, retrieve=None) -> list[dict]:
    """Source text behind a section, one retrieval per concept, deduplicated and kept whole.
    `documents` keeps other lectures out."""
    if retrieve is None:
        engine = _engine()
        retrieve = getattr(engine, "retrieve_chunks", None)


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


# Body paragraphs follow the source: about one per PARAGRAPH_SOURCE_CHARS of extracts, at most
# PARAGRAPH_CEILING, at least one per three concepts.
MIN_PARAGRAPHS = 2
PARAGRAPH_CEILING = 12
PARAGRAPH_SOURCE_CHARS = 650


def paragraph_range(n_concepts: int, source_chars: Optional[int] = None) -> tuple[int, int]:
    """(fewest, most) body paragraphs for `n_concepts` concepts and `source_chars` of extracts;
    without a length, the concept count decides."""
    by_concepts = max(MIN_PARAGRAPHS, -(-n_concepts // 3))
    if source_chars is None:
        least = min(PARAGRAPH_CEILING, by_concepts)
        return least, min(PARAGRAPH_CEILING, max(least, -(-n_concepts // 2)))
    by_length = max(MIN_PARAGRAPHS, source_chars // PARAGRAPH_SOURCE_CHARS)
    least = min(PARAGRAPH_CEILING, max(MIN_PARAGRAPHS, min(by_concepts, by_length)))
    most = max(least, min(PARAGRAPH_CEILING, by_length))
    return least, most


def build_write_prompt(section: dict, chunks: list[dict], other_concepts=(), other_picks=()) -> str:
    """The material half of the write prompt. Extracts come first: the model writes from
    whatever leads."""
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
        # Other sections' key points, not only their names: one chunk can hold a whole lecture, and
        # names alone left sections duplicating material.
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


def build_group_prompt(labelled_prompts: list[tuple[str, str]]) -> str:
    """Several sections' write prompts in one request, each delimited with its id. Each block's
    leave-out lists already name its siblings."""
    labels = [label for label, _ in labelled_prompts]
    lines = [f"This request holds {len(labels)} sections of one note, in note order: "
             f"{', '.join(labels)}. Write all of them, each under its id.", ""]
    for label, prompt in labelled_prompts:
        lines += [f"=== SECTION {label} ===", prompt, f"=== END SECTION {label} ===", ""]
    return "\n".join(lines)


# Labels too ordinary to police: banning 'has' or 'is a' would flag almost every section.
_LABEL_STOPWORDS = frozenset({"is", "are", "was", "were", "be", "been",
                              "has", "have", "had", "is a", "is an"})

_SENTENCE = re.compile(r"[^.!?]+[.!?]?")


def _label_pattern(label: str) -> Optional[re.Pattern]:
    """A regex catching one relation label used as a word: a single word with its inflections,
    anchored so 'causes' misses 'causality'."""
    if not label or label in _LABEL_STOPWORDS:
        return None
    if " " in label or len(label) < 6:
        return re.compile(rf"\b{re.escape(label)}\b")
    stem = re.sub(r"(?:ies|es|ed|ing|s)$", "", label)
    if len(stem) < 4:
        return re.compile(rf"\b{re.escape(label)}\b")
    return re.compile(rf"\b{re.escape(stem)}(?:e|es|s|ed|ing|d)?\b")


def normalise_unicode(value):
    """NFKC over every string in a nested structure: folds compatibility codepoints the model
    uses (OHM SIGN for omega) and PDF ligatures."""
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, list):
        return [normalise_unicode(v) for v in value]
    if isinstance(value, dict):
        return {k: normalise_unicode(v) for k, v in value.items()}
    return value


def paragraph_text(paragraph) -> str:
    """A body paragraph as flat text; a plain string (an older body) is accepted."""
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
    """Sentences restating a section relation: they hold its label and name both ends. Returns
    (label, sentence) pairs, one per label."""
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


# Section quiz: five asked for; under QUIZ_MIN_QUESTIONS surviving, the section is saved without
# one.
QUIZ_QUESTIONS = 5
QUIZ_MIN_QUESTIONS = 3
_QUIZ_DIFFICULTIES = ("easy", "medium", "hard")
_QUIZ_BLOOM = {"remember": "easy", "understand": "medium", "apply": "hard"}
_QUIZ_REF = re.compile(r"^\s*([PM])\s*(\d+)\s*$", re.I)


def _quiz_ref(raw, paragraphs: int, must_cover: int) -> Optional[dict]:
    """'P2' or 'M1' as {kind, index} (0-based), or None if it points nowhere; a bad ref costs
    the ref, not the question."""
    match = _QUIZ_REF.match(str(raw or ""))
    if not match:
        return None
    kind = "paragraph" if match.group(1).upper() == "P" else "must_cover"
    index = int(match.group(2)) - 1
    limit = paragraphs if kind == "paragraph" else must_cover
    return {"kind": kind, "index": index} if 0 <= index < limit else None


# Item-writing flaws: the first four drop a question; the last three are only counted, as they
# mostly fired on deliberate contrasts.
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


def validate_quiz(items, quiz_id: str, paragraphs: int, must_cover: int,
                  max_questions: int = QUIZ_QUESTIONS) -> tuple[Optional[list], int, dict]:
    """Checks a quiz in code: drops malformed questions and IWF_DROP flaws, counts IWF_LOG,
    keeps up to max_questions. Returns (questions or None, dropped, flags)."""
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
    dropped += max(0, len(kept) - max_questions)
    kept = kept[:max_questions]
    return (kept if len(kept) >= QUIZ_MIN_QUESTIONS else None), dropped, flags


def section_quiz_id(cache_key: str) -> str:
    """A section quiz's id from its write-cache key, so stored results find their quiz after a
    restart."""
    return "sec-" + hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:12]


def _retry_delay_seconds(exc: Exception) -> Optional[float]:
    """The retryDelay a quota error asks for in seconds, or None. Read off exc.details, so
    google.genai need not be imported."""
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
    """True for a failure worth retrying (busy model, full quota window). Matched on status or
    text, so google.genai need not be imported."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc).upper()
    return any(s in text for s in
               ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED",
                "INTERNAL", "429", "503"))


def _status_code(exc: Exception) -> Optional[int]:
    """The HTTP status the SDK attached to an error, or None (a timeout, a
    network failure)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _quota_ids(exc: Exception) -> list[str]:
    """The quotaId of every google.rpc.QuotaFailure violation on an error,
    e.g. "GenerateRequestsPerDayPerProjectPerModel-FreeTier"."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return []
    inner = details.get("error")
    items = (inner if isinstance(inner, dict) else details).get("details")
    found = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("QuotaFailure"):
            found += [str(v.get("quotaId", "")) for v in item.get("violations") or []
                      if isinstance(v, dict)]
    return found


def _is_daily_quota(exc: Exception) -> bool:
    """True for the per-day quota, read off the quotaId: its retryDelay ('13s') is no guide, as
    the quota resets at midnight Pacific."""
    return any("PerDay" in q for q in _quota_ids(exc))


class DailyLimitReached(Exception):
    """The note writer's model has used its daily request quota."""


DAILY_LIMIT_MESSAGE = ("The note writer has used today's request limit. "
                       "It resets at midnight Pacific time.")

# model -> epoch seconds until which writes are refused without a request after a per-day refusal.
# A fixed hour rather than the reset time, so no timezone data is needed.
_DAILY_LIMIT_UNTIL: dict[str, float] = {}
_DAILY_LIMIT_BLOCK_SECONDS = 3600


def _finish_reason(response) -> Optional[str]:
    """Why the model stopped (STOP, MAX_TOKENS, SAFETY, ...), or the prompt's
    block reason when no candidate came back."""
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        reason = getattr(candidates[0], "finish_reason", None)
        return getattr(reason, "name", None) or (str(reason) if reason is not None else None)
    feedback = getattr(response, "prompt_feedback", None)
    blocked = getattr(feedback, "block_reason", None)
    return f"no candidates (block_reason={getattr(blocked, 'name', blocked)})"


def _response_text(response) -> str:
    try:
        return response.text or ""
    except (AttributeError, ValueError):
        return ""


def _log_unusable(response, error) -> None:
    """Logs why an answer was unusable: finish reason, usage and the start of its text."""
    text = re.sub(r"\s+", " ", _response_text(response))
    print(f"Notes write: unusable response — {error} | finish_reason={_finish_reason(response)} "
          f"usage={_usage(response)} text[:500]={text[:500]!r}")


def _write_entry(section: dict, other_concepts=(), other_picks=()) -> dict:
    """
    What writing one section needs before any retrieval: the section as the
    writer sees it, its cache key, and what the note's other sections cover.
    """
    # Relations without a summary are left out: a bare label is often wrong, and the writer would
    # be asked to explain it.
    section = {**section, "relations": [
        e for e in section["relations"] if str(e.get("summary") or "").strip()
    ]}
    return {"section": section, "key": _write_key(section, other_concepts, other_picks),
            "others": list(other_concepts), "picks": list(other_picks)}


def _cached_body(cache_key: str) -> Optional[dict]:
    cached = _load_write_cache().get(cache_key)
    return cached if isinstance(cached, dict) and cached.get("lead") else None


async def _add_material(entry: dict, documents, retrieve=None) -> bool:
    """Retrieves a section's extracts and builds its prompt onto `entry`.
    False when nothing was retrieved, so there is nothing to write from."""
    section = entry["section"]
    chunks = await gather_chunks(section["concepts"], documents, retrieve=retrieve)
    if not chunks:
        print(f"Notes write: no source text retrieved for section {section['id']} "
              f"({len(section['concepts'])} concepts) — nothing to write from.")
        return False
    entry["chunks"] = chunks
    entry["prompt"] = build_write_prompt(section, chunks, entry["others"], entry["picks"])
    return True


async def write_body(section: dict, documents=None, other_concepts=(),
                     other_picks=(), chunk_retriever=None) -> tuple[Optional[dict], Optional[float]]:
    """Writes one section alone, through the write cache. Returns (body, retry_after); body is
    None on failure."""
    entry = _write_entry(section, other_concepts, other_picks)
    cached = _cached_body(entry["key"])
    if cached:
        return cached, None

    client = _gemini_client()
    if client is None:
        return None, None
    if not await _add_material(entry, documents, retrieve=chunk_retriever):
        return None, None

    body, retry_after = await _generate_body(client, entry["prompt"])
    if body is None:
        return None, retry_after
    return await _finish_body(entry, body), None


async def write_group(entries: list[tuple]) -> dict[int, tuple[Optional[dict], Optional[float], str]]:
    """Writes consecutive sections of one document in one call. Returns {id: (body, retry_after,
    status)}; an 'invalid' section is to be rewritten alone."""
    if len(entries) == 1:
        body, retry_after = await write_body(*entries[0])
        return {entries[0][0]["id"]: (body, retry_after, "written" if body is not None else "failed")}

    results, todo = {}, []
    for section, documents, others, picks in entries:
        entry = _write_entry(section, others, picks)
        cached = _cached_body(entry["key"])
        if cached:
            results[section["id"]] = (cached, None, "written")
        else:
            todo.append((entry, documents))

    client = _gemini_client() if todo else None
    ready = []
    for entry, documents in todo:
        if client is not None and await _add_material(entry, documents):
            ready.append(entry)
        else:
            results[entry["section"]["id"]] = (None, None, "failed")
    if not ready:
        return results

    if len(ready) == 1:
        entry = ready[0]
        body, retry_after = await _generate_body(client, entry["prompt"])
        results[entry["section"]["id"]] = (
            (await _finish_body(entry, body), None, "written") if body is not None
            else (None, retry_after, "failed"))
        return results

    labels = [f"S{i}" for i in range(1, len(ready) + 1)]
    prompt = build_group_prompt([(label, e["prompt"]) for label, e in zip(labels, ready)])
    bodies, retry_after = await _generate_group(client, prompt, labels)
    for label, entry in zip(labels, ready):
        section_id = entry["section"]["id"]
        if bodies is None:
            results[section_id] = (None, retry_after, "failed")
        elif bodies.get(label) is None:
            results[section_id] = (None, None, "invalid")
        else:
            results[section_id] = (await _finish_body(entry, bodies[label]), None, "written")
    logger.info("notes_write_group sections=%s written=%d invalid=%d failed=%d",
                [e["section"]["id"] for e in ready],
                *(sum(1 for e in ready if results[e["section"]["id"]][2] == s)
                  for s in ("written", "invalid", "failed")))
    return results


async def _finish_body(entry: dict, body: dict) -> dict:
    """Checks a new body, completes its stored shape and caches it under the section's own key."""
    section, chunks, cache_key = entry["section"], entry["chunks"], entry["key"]

    # Label breaches are recorded, not rewritten: a rewrite cost a quota request to change wording,
    # not facts.
    violations = find_label_violations(body, section["relations"])
    if violations:
        print(f"Notes write: section {section['id']} used "
              f"{len(violations)} relation label(s) as words "
              f"({', '.join(l for l, _ in violations)}) — recorded, not rewritten.")

    coverage = await measure_coverage(list(section.get("must_cover") or []), body)
    if coverage is not None:
        body["coverage"] = coverage
        logger.info("notes_write_coverage section=%s covered=%d weak=%d missing=%d total=%d",
                    section["id"], coverage["covered"], coverage["weak"],
                    coverage["missing"], coverage["total"])

    # Kept in the body's shape; no second call is made.
    body["rewritten"] = False
    body["label_violations"] = [label for label, _ in violations]
    body["sources"] = sorted({c.get("source") for c in chunks if c.get("source")})
    body["chunks_used"] = len(chunks)

    # A weak quiz is dropped, never retried.
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
    return body


def _usage(response) -> dict:
    """What the call spent, as the response reports it; thinking shares the 65,536-token
    response limit."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return {}
    fields = ("prompt_token_count", "candidates_token_count",
              "thoughts_token_count", "total_token_count")
    return {f: getattr(meta, f, None) for f in fields}


def _thinking_budget(sections: int) -> int:
    """thinking_budget for a call of `sections` sections. A gemini-2.5 setting: Gemini 3 takes
    thinking_level."""
    return min(_THINKING_BUDGET_MAX, _THINKING_PER_SECTION * sections)


def _group_output_estimate(sections: int) -> int:
    """Estimated response tokens (body, quiz and thinking) for a call
    writing `sections` sections."""
    return _OUTPUT_PER_SECTION * sections + _thinking_budget(sections)


def _write_timeout(sections: int) -> int:
    return _WRITE_TIMEOUT_SECONDS + _WRITE_TIMEOUT_PER_EXTRA_SECTION * (sections - 1)


# One Gemini write call at a time: at 5 requests a minute, parallel sections retried into the limit
# and spent the daily quota.
_GEMINI_CALL_LOCK = asyncio.Lock()


async def _call_gemini(client, prompt: str, system: str, schema, sections: int):
    """One Gemini request with at most one retry, after any call in flight. Returns (response or
    None, retry_after); raises DailyLimitReached."""
    from google.genai import types
    timeout = _write_timeout(sections)

    def call():
        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=schema,
                thinking_config=types.ThinkingConfig(thinking_budget=_thinking_budget(sections)),
                # No tools are passed, so the SDK's automatic function calling
                # has nothing to do; left on it warns on every single call.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )

    async with _GEMINI_CALL_LOCK:
        if _DAILY_LIMIT_UNTIL.get(GEMINI_MODEL, 0) > time.time():
            print(f"Notes write: {GEMINI_MODEL} hit its daily quota within the last hour — not calling.")
            raise DailyLimitReached(GEMINI_MODEL)

        waited = 0.0
        for attempt in range(_WRITE_MAX_ATTEMPTS):
            last = attempt == _WRITE_MAX_ATTEMPTS - 1
            try:
                response = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout)
                return response, None
            except asyncio.TimeoutError:
                print(f"Notes write: call exceeded {timeout}s (status=timeout) — giving up.")
                return None, None
            except Exception as exc:
                status = _status_code(exc)
                if _is_daily_quota(exc):
                    # Stop at once: a retry is refused too, and still counts.
                    _DAILY_LIMIT_UNTIL[GEMINI_MODEL] = time.time() + _DAILY_LIMIT_BLOCK_SECONDS
                    print(f"Notes write: daily quota reached (status={status}, "
                          f"quotaId={', '.join(_quota_ids(exc))}) — not retrying.")
                    raise DailyLimitReached(GEMINI_MODEL) from exc
                if last or not _is_transient(exc):
                    print(f"Notes write: Gemini call failed (status={status}) — {type(exc).__name__}: {exc}")
                    return None, _retry_delay_seconds(exc)
                asked = _retry_delay_seconds(exc)
                delay = asked if asked is not None else _WRITE_RETRY_BASE_SECONDS * (3 ** attempt)
                if waited + delay > _WRITE_RETRY_BUDGET_SECONDS:
                    # Printed whole: the QuotaFailure details say which quota was hit.
                    print(f"Notes write: {type(exc).__name__} (status={status}) on attempt "
                          f"{attempt + 1}/{_WRITE_MAX_ATTEMPTS}, next wait {delay}s would pass the "
                          f"{_WRITE_RETRY_BUDGET_SECONDS}s retry budget — giving up. {exc}")
                    return None, asked
                source = "server asked for" if asked is not None else "backing off"
                print(f"Notes write: {type(exc).__name__} (status={status}) on attempt "
                      f"{attempt + 1}/{_WRITE_MAX_ATTEMPTS} — {source} {delay}s.")
                await asyncio.sleep(delay)
                waited += delay
    return None, None


async def _generate_body(client, prompt: str) -> tuple[Optional[dict], Optional[float]]:
    """One Gemini call for a lone section body. Returns (body or None, retry_after)."""
    response, retry_after = await _call_gemini(client, prompt, _WRITE_SYSTEM, _WriteBody, 1)
    if response is None:
        return None, retry_after

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        # The schema is enforced server-side, so this is rare; parse the text rather than lose the
        # answer.
        text = _response_text(response)
        try:
            parsed = _WriteBody.model_validate_json(text)
        except (ValidationError, AttributeError) as exc:
            # A malformed quiz must not cost the section: try again without it.
            try:
                raw = json.loads(text)
                raw.pop("quiz", None)
                parsed = _WriteBody.model_validate(raw)
                print(f"Notes write: quiz unusable, section kept without it — {exc}")
            except (ValidationError, ValueError, TypeError, AttributeError):
                _log_unusable(response, exc)
                return None, None

    body = normalise_unicode(
        parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
    )
    _record_usage(body, response)
    return body, None


def _record_usage(body: dict, response) -> None:
    used = _usage(response)
    if used:
        logger.debug(
            "notes_write_usage in=%s out=%s thoughts=%s total=%s",
            used.get("prompt_token_count"), used.get("candidates_token_count"),
            used.get("thoughts_token_count"), used.get("total_token_count"),
        )
        # Kept on the body, which is on disk; a grouped section carries the whole call's usage.
        body["usage"] = dict(used)


def _section_from(item: dict) -> Optional[dict]:
    """A grouped answer's section as a body, or None without a lead or paragraphs. A malformed
    quiz costs only the quiz."""
    raw = {k: v for k, v in item.items() if k != "id"}
    try:
        parsed = _WriteBody.model_validate(raw)
    except ValidationError as exc:
        try:
            parsed = _WriteBody.model_validate({k: v for k, v in raw.items() if k != "quiz"})
            print(f"Notes write: quiz unusable, section kept without it — {exc}")
        except ValidationError:
            return None
    body = normalise_unicode(parsed.model_dump())
    if not str(body.get("lead") or "").strip() or not body.get("body"):
        return None
    return body


async def _generate_group(client, prompt: str, labels: list[str]):
    """One Gemini call for several sections. Returns (bodies, retry_after): label to body or
    None, or bodies None when no answer came."""
    response, retry_after = await _call_gemini(client, prompt, _WRITE_SYSTEM + _GROUP_SYSTEM,
                                               _WriteGroup, len(labels))
    if response is None:
        return None, retry_after

    parsed = getattr(response, "parsed", None)
    if parsed is not None and getattr(parsed, "sections", None) is not None:
        items = [s.model_dump() for s in parsed.sections]
    else:
        # The SDK parses all or nothing; read the sections one by one.
        try:
            raw = json.loads(_response_text(response))
        except ValueError:
            raw = None
        items = raw.get("sections") if isinstance(raw, dict) else None

    bodies = {label: None for label in labels}
    if not isinstance(items, list):
        _log_unusable(response, "no sections list in the grouped answer")
        return bodies, None
    for item in items:
        label = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
        if label in bodies and bodies[label] is None:
            body = _section_from(item)
            if body is not None:
                _record_usage(body, response)
            bodies[label] = body
    missing = [label for label, body in bodies.items() if body is None]
    if missing:
        _log_unusable(response, f"no usable section for {', '.join(missing)} of {len(labels)}")
    return bodies, None


# --- routes ---

class PlanRequest(BaseModel):
    document_ids: list[int] = Field(min_length=1)
    days: Optional[int] = Field(default=None, ge=1, le=30)