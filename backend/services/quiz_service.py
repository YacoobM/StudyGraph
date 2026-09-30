"""
Quiz generation from the extracted concept graph.

    GET  /api/quiz                                 one entry per document, no generation
    POST /api/quiz/documents/{doc_id}/generate     generate that document's quiz
    POST /api/quiz/results                         record an attempt (document or section quiz)
    GET  /api/quiz/results                         attempts summarised per quiz

An LLM (Groq) writes the questions. When the call fails or returns too few
usable questions the quiz is marked "failed" and the reader can retry. There
is no template fallback: it turned raw edge labels into questions ("what
does microeconomics part_of?"), often with the direction wrong.

A document's quiz is generated only when asked for — opening the Quiz tab
used to start one LLM call per week on every visit. Each is cached keyed by
a fingerprint of the document's concepts, generates in the background, and
reports status "generating" until ready.

Section quizzes are not generated here: the notes write call returns them
with the section body (notes_service.validate_quiz). This module only stores
their attempts, alongside the document quizzes', in RESULTS_FILE.

Mount:
    from services.quiz_service import router as quiz_router, set_graph_source
    app.include_router(quiz_router)
    set_graph_source(lambda: engine)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import EXTRACTION_MODEL, OUTPUT_DIR
from api.routes.auth import current_user
from models.quiz_models import QuizAttempt
from repositories.database import connect
from services.llm_gateway import estimate_tokens, limiter

router = APIRouter(tags=["quiz"])
logger = logging.getLogger(__name__)


class QuizConfig:
    # Six, not twelve: at max_tokens 900 six questions measured 583 tokens,
    # and the model's per-minute output limit is 1,000.
    POOL_SIZE_PER_TOPIC = 6
    LLM_MAX_TOKENS = 900
    MIN_CONCEPTS_PER_TOPIC = 4
    MIN_POOL_TO_SHOW_TOPIC = 2
    LLM_TIMEOUT_SECONDS = 45
    LLM_MODEL = EXTRACTION_MODEL
    LLM_MAX_CONCEPTS = 40
    LLM_MAX_RELATIONS = 40
    POLL_HINT_SECONDS = 4


DIFFICULTIES = ("easy", "medium", "hard")

_graph_source: Optional[Callable] = None


def set_graph_source(fn: Callable) -> None:
    global _graph_source
    _graph_source = fn


def _graph():
    if _graph_source is None:
        return [], []
    engine = _graph_source()
    if engine is None:
        return [], []
    return list(getattr(engine, "concepts", []) or []), list(getattr(engine, "edges", []) or [])


def _week_num(week: str) -> int:
    digits = re.sub(r"\D+", "", str(week or ""))
    return int(digits) if digits else 1


def _question_id(topic_id: str, question: str) -> str:
    digest = hashlib.sha1(f"{topic_id}:{question}".encode()).hexdigest()[:10]
    return f"{topic_id}-{digest}"


def _topic_fingerprint(names: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(names)).encode()).hexdigest()


def shuffle_quiz(questions: list[dict]) -> list[dict]:
    """
    The questions with each one's options in a fixed random order and `a`
    pointing at the same text.

    Models put the answer first far more often than chance (12 of 12 on two
    document quizzes), so the order they write cannot be the order shown.
    Quizzes are stored as the model wrote them and shuffled here, on every
    path out of the backend. The order is seeded by the question's id, so the
    same question comes out the same way every time — a stored attempt's
    answer indices keep meaning the same options.
    """
    out = []
    for q in questions or []:
        options = list(q.get("o") or [])
        answer = q.get("a")
        if not isinstance(answer, int) or not 0 <= answer < len(options):
            out.append(q)
            continue
        seed = hashlib.sha1(str(q.get("id") or q.get("q") or "").encode("utf-8")).hexdigest()[:12]
        order = list(range(len(options)))
        random.Random(int(seed, 16)).shuffle(order)
        out.append({**q, "o": [options[i] for i in order], "a": order.index(answer)})
    return out


def pass_mark(total: int, kind: str = "section") -> int:
    """
    Correct answers needed to pass a quiz of `total` questions.

    A section quiz allows one miss: 2 of 3, 3 of 4, 4 of 5. A fixed 4/5 share
    asked for every question on a three- or four-question quiz. A document
    quiz keeps its two-thirds mastery rule, as a count.
    """
    if total <= 0:
        return 1
    if kind == "section":
        return total - 1 if total >= 3 else total
    return -(-2 * total // 3)


def topic_edges(edges: list[dict], names) -> list[dict]:
    """
    The edges a topic's questions may use: those between its own concepts, or,
    when there are none, those touching at least one of them. Never the whole
    graph — that fallback put other lectures' relations into a document's
    quiz. Empty when nothing touches the topic; the questions then come from
    the concepts alone.
    """
    names = set(names)
    inside = [e for e in edges if e["from"] in names and e["to"] in names]
    return inside or [e for e in edges if e["from"] in names or e["to"] in names]


LLM_PROMPT = """You are writing revision questions for a student, using ONLY the
concepts and relationships extracted from their own lecture material below.

Concepts: {concepts}

Relationships:
{relations}

Write {n} multiple-choice questions. Rules:
- Ask only about the concepts and relationships given. Invent nothing.
- Exactly 4 options each, one correct.
- Wrong options must be other concepts from the list, so they are plausible.
- "d" is the difficulty: "easy", "medium" or "hard".
- "why" explains the answer in one sentence.

Return ONLY a JSON array, no prose and no markdown fences:
[{{"q":"...","o":["...","...","...","..."],"a":0,"why":"...","d":"easy"}}]
"""


def _questions_from_llm(concepts: list[str], edges: list[dict], n: int) -> list[dict]:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return []
    try:
        from groq import Groq
    except ImportError:
        return []

    relations = "\n".join(
        f"- {e['from']} {e.get('relation', 'relates to')} {e['to']}"
        for e in edges[:QuizConfig.LLM_MAX_RELATIONS]
    ) or "(none recorded — write the questions from the concepts alone)"
    prompt = LLM_PROMPT.format(
        concepts=", ".join(concepts[:QuizConfig.LLM_MAX_CONCEPTS]), relations=relations, n=n,
    )

    # Minimum fix until the chunk-grounded quiz replaces this call. Reasoning
    # "low" spent up to 1,468 tokens thinking and was cut off at 2,048 before
    # the JSON closed, which came back as a quiz with no questions. The SDK's
    # own retry is off so a 429 is handled once, here, with the server's wait.
    from groq import RateLimitError
    from services.graph_fusion import is_request_too_large, retry_after_seconds

    client = Groq(api_key=key, timeout=QuizConfig.LLM_TIMEOUT_SECONDS, max_retries=0)
    finish = None
    for attempt in range(2):
        try:
            limiter.acquire_sync(estimate_tokens(prompt), QuizConfig.LLM_MAX_TOKENS)
            resp = client.chat.completions.create(
                model=QuizConfig.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                reasoning_effort="none",
                max_tokens=QuizConfig.LLM_MAX_TOKENS,
            )
            finish = resp.choices[0].finish_reason
            text = (resp.choices[0].message.content or "").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            items = json.loads(text)
            break
        except RateLimitError as e:
            if is_request_too_large(e) or attempt == 1:
                logger.warning("document_quiz_failed reason=rate_limited too_large=%s", is_request_too_large(e))
                return []
            wait = retry_after_seconds(e)
            wait = min(wait if wait is not None else 5.0, QuizConfig.LLM_TIMEOUT_SECONDS)
            logger.warning("document_quiz_rate_limited retry_in=%.1fs", wait)
            time.sleep(wait)
        except json.JSONDecodeError as e:
            logger.warning("document_quiz_failed reason=unparseable_json finish_reason=%s error=%s", finish, e)
            return []
        except ValueError as e:
            logger.warning("document_quiz_failed reason=over_token_budget error=%s", e)
            return []
        except Exception as e:
            logger.warning("document_quiz_failed reason=%s error=%s", type(e).__name__, e)
            return []

    good = []
    for it in items if isinstance(items, list) else []:
        if (
            isinstance(it, dict)
            and isinstance(it.get("o"), list) and len(it["o"]) >= 3
            and isinstance(it.get("a"), int) and 0 <= it["a"] < len(it["o"])
            and it.get("q") and it.get("why")
        ):
            it["d"] = it.get("d") if it.get("d") in DIFFICULTIES else "medium"
            good.append(it)
    if not isinstance(items, list) or len(good) < len(items):
        # Valid JSON in the wrong shape: log what the model sent, so a run's
        # log shows the actual shape rather than just a count.
        body = re.sub(r"\s+", " ", text)
        logger.warning("document_quiz_unexpected_shape step=document_quiz kept=%d of=%s finish_reason=%s body=%s",
                       len(good), len(items) if isinstance(items, list) else type(items).__name__,
                       finish, body[:500] + ("…" if len(body) > 500 else ""))
    if not good:
        logger.warning("document_quiz_failed reason=no_usable_questions finish_reason=%s", finish)
    return good


# topic_id -> {"fingerprint": str, "topic": dict}. Keyed per topic, not per
# whole-graph size, so uploading new material only invalidates the topics
# whose own concepts changed.
_TOPIC_CACHE: dict[str, dict] = {}
_GENERATING: set[str] = set()
_TASKS: set[asyncio.Task] = set()   # keeps background tasks alive until done


async def _generate_topic(topic_id: str, week: str, members: list[dict],
                           edges: list[dict], names: list[str], fingerprint: str,
                           title: Optional[str] = None) -> None:
    try:
        week_edges = topic_edges(edges, names)
        pool = await asyncio.to_thread(_questions_from_llm, names, week_edges, QuizConfig.POOL_SIZE_PER_TOPIC)
        for q in pool:
            q["id"] = _question_id(topic_id, q["q"])

        sources = sorted({c.get("source", "") for c in members if c.get("source")})
        topic = {
            "id": topic_id,
            "day": _week_num(week),
            "name": title or (f"{week} — {names[0]}" if names else week),
            "desc": f"{len(names)} concepts from {', '.join(sources) or 'your uploads'}.",
            # Too few usable questions is a failed call, not a quiz: the reader
            # is offered a retry rather than two or three stray questions.
            "status": "ready" if len(pool) >= QuizConfig.MIN_POOL_TO_SHOW_TOPIC else "failed",
            "questions": pool,
        }
        _TOPIC_CACHE[topic_id] = {"fingerprint": fingerprint, "topic": topic}
    finally:
        _GENERATING.discard(topic_id)


def _user_documents(user_id) -> list:
    """The user's document rows, oldest week first. A seam, so tests can
    supply rows without a database."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, filename, week, status FROM documents WHERE user_id = ? "
            "ORDER BY uploaded_at", (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# A title line: a short run of words, not a page number, URL, date or
# copyright notice. Checked against the first lines of the document's text.
_TITLE_MAX_CHARS = 90
_TITLE_SKIP = re.compile(
    r"^(?:page\s*\d+|\d+(?:\s*/\s*\d+)?|https?://\S+|www\.\S+|©.*|copyright\b.*|"
    r"mit opencourseware.*|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})$", re.I)
_TITLE_LEAD = re.compile(r"^(?:lecture|chapter|week|topic|unit|module|part|session|lesson)\b", re.I)


def clean_filename(filename: str) -> str:
    """'mit14_01_f23_lec1.pdf' -> 'mit14 01 f23 lec1': extension off,
    underscores and runs of separators to single spaces."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(filename or "").strip())
    return re.sub(r"[\s_]+", " ", stem).strip() or str(filename or "")


def _title_from_text(text: str) -> Optional[str]:
    """
    The document's own title, read cheaply from the start of its stored text.

    Only the first dozen non-empty lines are looked at. A line opening with
    Lecture / Chapter / Week and similar wins, joined with the next line when
    it is only a label ("Lecture 1" then "Introduction to ..."). Otherwise the
    first line that reads like a title: two to fourteen words, mostly letters,
    not a page number, URL, date or notice. None when nothing qualifies.
    """
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in str(text or "").splitlines()]
    lines = [ln for ln in lines if ln][:12]

    def usable(line):
        words = line.split()
        letters = sum(ch.isalpha() for ch in line)
        return (not _TITLE_SKIP.match(line) and len(line) <= _TITLE_MAX_CHARS
                and 1 <= len(words) <= 14 and letters >= max(3, len(line) // 2))

    for i, line in enumerate(lines):
        if _TITLE_LEAD.match(line) and usable(line):
            label_only = re.fullmatch(r"\w+\s*[\d.]+[:.]?", line)
            if label_only and i + 1 < len(lines) and usable(lines[i + 1]):
                return f"{line.rstrip(':.')}: {lines[i + 1]}"
            return line
    for line in lines:
        if usable(line) and len(line.split()) >= 2:
            return line
    return None


def document_title(filename: str) -> str:
    """What the Quiz tab calls a document: its own title line when the stored
    text has one, else the cleaned filename. Never the raw filename."""
    engine = _graph_source() if _graph_source else None
    chunks = getattr(engine, "chunks_by_id", None) or {}
    first = None
    for chunk_id, chunk in chunks.items():
        if chunk.get("source") != filename:
            continue
        try:
            index = int(str(chunk_id).rsplit(":", 2)[1])
        except (IndexError, ValueError):
            index = 0
        if first is None or index < first[0]:
            first = (index, chunk.get("text") or "")
    return (_title_from_text(first[1]) if first else None) or clean_filename(filename)


def _document_quiz(doc: dict, concepts: list[dict]) -> dict:
    """
    One document's quiz entry: what it would be generated from and how far
    it has got. Generates nothing.

    Members are the engine's concept records for this file. The engine keeps
    one record per mention per document, so a concept taught in two lectures
    is in both documents' quizzes.
    """
    members = [c for c in concepts if c.get("source") == doc["filename"]]
    names = sorted({c["name"] for c in members})
    topic_id = f"doc-{doc['id']}"
    entry = {
        "document_id": doc["id"],
        "filename": doc["filename"],
        "title": document_title(doc["filename"]),
        "week": doc.get("week") or (members[0].get("week") if members else None),
        "concepts": len(names),
        "quiz_id": topic_id,
        "status": "idle",
        "topic": None,
    }
    if len(names) < QuizConfig.MIN_CONCEPTS_PER_TOPIC:
        entry["status"] = "too_small"
        return entry
    cached = _TOPIC_CACHE.get(topic_id)
    if cached and cached["fingerprint"] == _topic_fingerprint(names):
        topic = cached["topic"]
        entry["status"] = topic["status"]
        # Stored as generated; shuffled on the way out, the same way each time.
        # pass_marks covers every set the Quiz tab's difficulty filter can
        # show, so the client never computes a pass mark itself.
        questions = topic["questions"]
        pass_marks = {"all": pass_mark(len(questions), "document")}
        for d in DIFFICULTIES:
            pass_marks[d] = pass_mark(sum(1 for q in questions if q.get("d") == d), "document")
        entry["topic"] = {**topic, "questions": shuffle_quiz(questions), "pass_marks": pass_marks}
    elif topic_id in _GENERATING:
        entry["status"] = "generating"
    return entry


@router.get("/api/quiz")
async def get_quiz(user=Depends(current_user)):
    """Every document with the state of its quiz. Nothing is generated here —
    that waits for POST /api/quiz/documents/{id}/generate."""
    concepts, _edges = _graph()
    documents = [_document_quiz(d, concepts) for d in _user_documents(user["id"])]
    reason = None
    if not documents:
        reason = "No documents yet. Upload one and it will be listed here."
    elif not concepts:
        reason = "No documents have been processed yet."
    return {
        "documents": documents,
        "reason": reason,
        "poll_after_seconds": QuizConfig.POLL_HINT_SECONDS,
    }


@router.post("/api/quiz/documents/{doc_id}/generate")
async def generate_document_quiz(doc_id: int, user=Depends(current_user)):
    """Starts one document's quiz in the background, unless it is already
    cached for the current concepts or already running. Returns its entry;
    poll GET /api/quiz until the status leaves "generating"."""
    doc = next((d for d in _user_documents(user["id"]) if d["id"] == doc_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="That document is not in your library.")

    concepts, edges = _graph()
    entry = _document_quiz(doc, concepts)
    if entry["status"] == "too_small":
        raise HTTPException(status_code=422,
                            detail="This document has too few concepts for a quiz.")
    if entry["status"] in ("idle", "failed"):
        members = [c for c in concepts if c.get("source") == doc["filename"]]
        names = sorted({c["name"] for c in members})
        topic_id = entry["quiz_id"]
        _TOPIC_CACHE.pop(topic_id, None)
        _GENERATING.add(topic_id)
        task = asyncio.create_task(_generate_topic(
            topic_id, members[0].get("week") or "Week 1", members, edges, names,
            _topic_fingerprint(names), title=entry["title"],
        ))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        entry = {**entry, "status": "generating", "topic": None}
    return {"document": entry, "poll_after_seconds": QuizConfig.POLL_HINT_SECONDS}


@router.get("/api/documents/stats")
async def document_stats(user=Depends(current_user)):
    concepts, edges = _graph()
    if not concepts:
        return {"stats": {}}

    by_source: dict[str, dict] = {}
    names_by_source: dict[str, set] = defaultdict(set)

    for c in concepts:
        src = c.get("source") or "unknown"
        names_by_source[src].add(c["name"])
        entry = by_source.setdefault(src, {"concepts": 0, "edges": 0, "week": c.get("week")})
        entry["week"] = entry["week"] or c.get("week")

    for src, names in names_by_source.items():
        by_source[src]["concepts"] = len(names)
        by_source[src]["edges"] = sum(1 for e in edges if e["from"] in names and e["to"] in names)

    return {"stats": by_source}


# ---------------------------------------------------------------------------
# results
#
# Every attempt is kept, per user, in one JSON file: which quiz, the option
# picked for each question, the score and when. Summaries (best score,
# attempt count, last attempt) are computed from the list on read, so nothing
# stored has to be kept in step with anything else.
# ---------------------------------------------------------------------------

RESULTS_FILE = OUTPUT_DIR / "quiz_results.json"
_RESULTS_LOCK = threading.Lock()


def _load_results() -> dict:
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _append_attempt(user_id, attempt: dict) -> list[dict]:
    """Adds one attempt and returns the user's attempts at that quiz. The file
    is re-read under the lock, so two saves landing together both survive."""
    with _RESULTS_LOCK:
        data = _load_results()
        attempts = data.setdefault(str(user_id), [])
        attempts.append(attempt)
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RESULTS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, RESULTS_FILE)
    return [a for a in attempts if a["quiz_id"] == attempt["quiz_id"]]


def summarise_attempts(attempts: list[dict]) -> dict:
    """One quiz's attempts as the Quiz tab lists them. Best is the highest
    share correct, the later attempt breaking ties; last is the most recent.
    Quizzes carry no points: attempts saved before they were removed may
    still hold a "points" field, and it is ignored."""
    best = max(attempts, key=lambda a: (a["correct"] / a["total"], a["at"]))
    last = max(attempts, key=lambda a: a["at"])
    return {
        "quiz_id": last["quiz_id"],
        "kind": last["kind"],
        "title": last.get("title"),
        "group": last.get("group"),
        "attempts": len(attempts),
        "best": {"correct": best["correct"], "total": best["total"]},
        "passed": any(a["passed"] for a in attempts),
        "last_at": last["at"],
        "last": {"correct": last["correct"], "total": last["total"],
                 "answers": last["answers"]},
    }


@router.post("/api/quiz/results")
async def save_result(body: QuizAttempt, user=Depends(current_user)):
    if body.total <= 0:
        raise HTTPException(status_code=422, detail="A quiz needs at least one question.")
    if body.correct > body.total or len(body.answers) > body.total:
        raise HTTPException(status_code=422, detail="That score does not fit the quiz.")

    passed = body.correct >= pass_mark(body.total, body.kind)
    attempt = {
        "quiz_id": body.quiz_id, "kind": body.kind, "title": body.title, "group": body.group,
        "answers": list(body.answers), "correct": body.correct, "total": body.total,
        "passed": passed, "at": time.time(),
    }
    mine = await asyncio.to_thread(_append_attempt, user["id"], attempt)
    return {"result": summarise_attempts(mine), "passed": passed}


@router.get("/api/quiz/results")
async def list_results(user=Depends(current_user)):
    by_quiz: dict[str, list[dict]] = defaultdict(list)
    for attempt in _load_results().get(str(user["id"]), []):
        by_quiz[attempt["quiz_id"]].append(attempt)
    return {"results": {qid: summarise_attempts(a) for qid, a in by_quiz.items()}}


def passed_quiz_ids(user_id, kind: str = "section") -> set[str]:
    """Quiz ids of that kind the user has passed at least once. Notes read this
    to tick sections, so completion survives a reload and a restart."""
    return {a["quiz_id"] for a in _load_results().get(str(user_id), [])
            if a.get("kind") == kind and a.get("passed")}


# ---------------------------------------------------------------------------
# section quizzes
#
# Notes live in memory and are gone after a restart; their quizzes must not
# be. Each section quiz a user has been shown is recorded here with its note
# and section titles, keyed by quiz id, and the Quiz tab's "From notes" list
# is built from this file alone. Per user, because the write cache it comes
# from is shared by everyone.
# ---------------------------------------------------------------------------

SECTION_QUIZ_FILE = OUTPUT_DIR / "section_quizzes.json"
_SECTION_QUIZ_LOCK = threading.Lock()


def _load_section_quizzes() -> dict:
    try:
        with open(SECTION_QUIZ_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def register_section_quiz(user_id, quiz_id: str, note_title: str, section_title: str,
                          index: int, questions: list[dict]) -> None:
    """Records one section quiz for a user. Skips the write when nothing
    changed, since this runs every time a section is opened. Never raises —
    a quiz that cannot be recorded must not fail the section."""
    entry = {"quiz_id": quiz_id, "note_title": note_title, "section_title": section_title,
             "index": index, "questions": questions}
    try:
        with _SECTION_QUIZ_LOCK:
            data = _load_section_quizzes()
            mine = data.setdefault(str(user_id), {})
            old = mine.get(quiz_id)
            if old and {k: v for k, v in old.items() if k != "saved_at"} == entry:
                return
            mine[quiz_id] = {**entry, "saved_at": time.time()}
            SECTION_QUIZ_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SECTION_QUIZ_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SECTION_QUIZ_FILE)
    except OSError as exc:
        print(f"Quiz: section quiz {quiz_id} could not be recorded — {exc}")


@router.get("/api/quiz/sections")
async def list_section_quizzes(user=Depends(current_user)):
    """Every section quiz the user has been given, grouped by note title and
    in section order. Built from SECTION_QUIZ_FILE, not from the notes in
    memory, so it is the same before and after a restart."""
    by_note: dict[str, list[dict]] = defaultdict(list)
    for entry in _load_section_quizzes().get(str(user["id"]), {}).values():
        by_note[entry.get("note_title") or "Revision notes"].append(entry)
    notes = []
    for title, entries in by_note.items():
        entries.sort(key=lambda e: (e.get("index", 0), e.get("section_title") or ""))
        notes.append({
            "title": title,
            "latest": max(e.get("saved_at", 0) for e in entries),
            # Questions were stored already shuffled by register_section_quiz.
            "sections": [{"quiz_id": e["quiz_id"], "title": e.get("section_title"),
                          "questions": e.get("questions") or [],
                          "pass_mark": pass_mark(len(e.get("questions") or []))} for e in entries],
        })
    notes.sort(key=lambda n: -n["latest"])
    return {"notes": notes}
