"""Quiz routes: document quizzes written by LLM from each document's text, and attempt records for document and section quizzes (those come from the notes write call)."""

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

from app.config import (COMPLETION_TOKEN_PARAMETER, EXTRACTION_MODEL, LLM_API_KEY_ENV,
                        NON_RAG_REASONING_EFFORT, OUTPUT_DIR)
from api.routes.auth import current_user
from models.quiz_models import QuizAttempt
from repositories.database import connect
from services.llm_gateway import estimate_tokens, limiter

router = APIRouter(tags=["quiz"])
logger = logging.getLogger(__name__)


class QuizConfig:
    # Six questions measured 583 output tokens; the model's output limit is 1,000 a minute.
    POOL_SIZE_PER_TOPIC = 6
    LLM_MAX_TOKENS = 900
    MIN_CONCEPTS_PER_TOPIC = 4
    MIN_POOL_TO_SHOW_TOPIC = 2
    LLM_TIMEOUT_SECONDS = 45
    LLM_MODEL = EXTRACTION_MODEL
    # Input cap in tokens: under the limiter's 7,000-a-minute input budget, with room for the
    # prompt and extraction.
    LLM_INPUT_TOKENS = 4000
    # Share of an evidence sentence's content words that must occur in one sentence or short run of
    # lines.
    EVIDENCE_OVERLAP = 0.8
    POLL_HINT_SECONDS = 4

PTS = {"easy": 10, "medium": 20, "hard": 30}

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


def _topic_fingerprint(names: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(names)).encode()).hexdigest()


def shuffle_quiz(questions: list[dict]) -> list[dict]:
    """Each question's options in a fixed random order seeded by its id. Models put the answer
    first far more often than chance (12 of 12)."""
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
    """Correct answers needed to pass: a section quiz allows one miss, a document quiz needs two
    thirds."""
    if total <= 0:
        return 1
    if kind == "section":
        return total - 1 if total >= 3 else total
    return -(-2 * total // 3)

def _distractors(answer: str, pool: list[str], exclude: set[str] | None = None) -> list[str]:
    blocked = {answer} | (exclude or set())
    others = [c for c in pool if c not in blocked]
    random.shuffle(others)
    return others[:QuizConfig.DISTRACTOR_COUNT]


def _mcq(question: str, answer: str, pool: list[str], why: str, diff: str,
         exclude: set[str] | None = None) -> Optional[dict]:
    wrong = _distractors(answer, pool, exclude)
    if len(wrong) < 2:
        return None
    options = wrong + [answer]
    random.shuffle(options)
    return {"q": question, "o": options, "a": options.index(answer), "why": why, "d": diff}

def _difficulty(degree: int) -> str:
    if degree >= QuizConfig.HUB_DEGREE_EASY:
        return "easy"
    if degree >= QuizConfig.HUB_DEGREE_MEDIUM:
        return "medium"
    return "hard"

def _questions_from_graph(concepts: list[dict], edges: list[dict], week: str) -> list[dict]:
    names = sorted({c["name"] for c in concepts if c.get("week") == week})
    if len(names) < QuizConfig.MIN_CONCEPTS_PER_TOPIC:
        names = sorted({c["name"] for c in concepts})
    if len(names) < QuizConfig.MIN_CONCEPTS_PER_TOPIC:
        return []

    degree = defaultdict(int)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1

    week_edges = [e for e in edges if e["from"] in names and e["to"] in names] or edges
    out: list[dict] = []

    for e in week_edges:
        rel = (e.get("relation") or "relates to").strip()
        src, tgt = e["from"], e["to"]

        q = _mcq(f"According to your sources, what does {src} {rel}?", tgt, names,
                 f"Your material records: {src} {rel} {tgt}.", _difficulty(degree[tgt]), {src})
        if q:
            out.append(q)

        q2 = _mcq(f"Which concept {rel} {tgt}?", src, names,
                  f"Your material records: {src} {rel} {tgt}.", _difficulty(degree[src]), {tgt})
        if q2:
            out.append(q2)

        if len(out) >= QuizConfig.POOL_SIZE_PER_TOPIC:
            break

    if degree:
        hub = max(names, key=lambda n: degree[n], default=None)
        if hub:
            q3 = _mcq("Which concept is the most connected in this part of your material?",
                      hub, names,
                      f"{hub} appears in {degree[hub]} of the relationships extracted from your document.",
                      "medium")
            if q3:
                out.append(q3)

    random.shuffle(out)
    return out[:QuizConfig.POOL_SIZE_PER_TOPIC]


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
    key = os.getenv(LLM_API_KEY_ENV)
    if not key:
        return []
    relations = "\n".join(
        f"- {e['from']} {e.get('relation', 'relates to')} {e['to']}"
        for e in edges[:QuizConfig.LLM_MAX_RELATIONS]
    )
    prompt = LLM_PROMPT.format(
        concepts=", ".join(concepts[:QuizConfig.LLM_MAX_CONCEPTS]), relations=relations, n=n,
    )

    try:
        from services.llm_provider import create_llm_client
        client = create_llm_client(
            key,
            timeout=QuizConfig.LLM_TIMEOUT_SECONDS,
            max_retries=1,
        )
        resp = client.chat.completions.create(
            model=QuizConfig.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            reasoning_effort=NON_RAG_REASONING_EFFORT,
        )
        text = resp.choices[0].message.content.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        items = json.loads(text)
    except Exception:
        return []

    good = []
    for it in items if isinstance(items, list) else []:
        if (
            isinstance(it, dict)
            and isinstance(it.get("o"), list) and len(it["o"]) >= 3
            and isinstance(it.get("a"), int) and 0 <= it["a"] < len(it["o"])
            and it.get("q") and it.get("why")
        ):
            it["d"] = it.get("d") if it.get("d") in PTS else "medium"
            good.append(it)
    return good


# --- document quiz, from the document's text: graph labels turned reversed edges into facts ---

_QUIZ_SYSTEM = (
    "You write multiple-choice revision questions for a student from the SOURCE TEXT only.\n"
    "Rules:\n"
    "- Every question tests a fact the text states. Do not use outside knowledge.\n"
    "- 4 options. Exactly one is correct according to the text. Each wrong option must be "
    "false according to the text, not merely unmentioned, and must not also be a correct answer.\n"
    "- Options have similar length and grammatical form. No 'all of the above' or 'none of the "
    "above'. Do not repeat the answer's wording in the question.\n"
    "- Never ask two questions about the same fact.\n"
    "- d is the difficulty: easy, medium or hard. why: one sentence saying why the answer is right.\n"
    "- evidence: copy the one sentence from the text that proves the answer.\n"
    'Return only JSON: {"questions": [{"q": "...", "o": ["...", "...", "...", "..."], "a": 0, '
    '"why": "...", "d": "easy", "evidence": "..."}]}'
)

_URL_OR_MAIL = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.-]+\b|\u00a9")


def quiz_source_text(text: str) -> str:
    """Content lines a quiz may ask about, minus lines with a URL, email or copyright sign and a
    heading just above one. content_text keeps a footer printed once."""
    from services import key_sentences
    lines = key_sentences.content_text(text).splitlines()
    drop = {i for i, line in enumerate(lines) if _URL_OR_MAIL.search(line)}
    for i in sorted(drop):
        if i > 0 and i - 1 not in drop and key_sentences._is_heading(lines[i - 1]):
            drop.add(i - 1)
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def sample_text(text: str, cap_chars: int, windows: int = 8) -> str:
    """Up to cap_chars of text: all of it, or `windows` equal stretches from evenly spaced
    points."""
    if len(text) <= cap_chars:
        return text
    lines = text.splitlines()
    per = cap_chars // windows
    parts = []
    for w in range(windows):
        taken, size = [], 0
        for line in lines[w * len(lines) // windows:]:
            if size + len(line) + 1 > per and taken:
                break
            taken.append(line[:per])
            size += len(line) + 1
        parts.append("\n".join(taken))
    return "\n...\n".join(p for p in parts if p)


def _evidence_units(text: str) -> list[set]:
    """Content-word sets of each sentence and of each run of up to 3 lines, since slides split
    sentences across bullets."""
    from services import notes_service as ns
    lines = [ln.strip() for ln in ns._prepare_text(text).splitlines() if ln.strip()]
    units = []
    for i in range(len(lines)):
        for span in range(1, 4):
            if i + span <= len(lines):
                units.append(" ".join(lines[i:i + span]))
    units += [s for ln in lines for s in re.split(r"(?<=[.!?;])\s+", ln)]
    return [w for w in (ns._content_words(u) for u in units) if w]


def evidence_supported(evidence: str, units: list[set]) -> bool:
    """Whether quoted evidence is in the text: EVIDENCE_OVERLAP of its content words in one
    unit. Not verbatim, since the model fixes broken ligatures when quoting."""
    from services import notes_service as ns
    words = ns._content_words(ns._prepare_text(evidence or ""))
    if not words:
        return False
    return any(len(words & unit) / len(words) >= QuizConfig.EVIDENCE_OVERLAP for unit in units)


_QUIZ_FIELDS = ("id", "q", "o", "a", "why", "d")


def _questions_from_document(filename: str, quiz_id: str, n: int) -> list[dict]:
    """One call for a document's quiz from its text, on the configured provider. Questions whose
    evidence is not in the text, or that fail validate_quiz, are dropped."""
    key = os.getenv(LLM_API_KEY_ENV)
    if not key:
        return []
    from services import notes_service as ns
    from services.graph_fusion import is_request_too_large, retry_after_seconds
    from services.llm_provider import RATE_LIMIT_ERRORS, create_llm_client

    source = quiz_source_text(ns.document_texts({filename}).get(filename, ""))
    if not source.strip():
        logger.warning("document_quiz_failed reason=no_document_text file=%s", filename)
        return []
    excerpt = sample_text(source, QuizConfig.LLM_INPUT_TOKENS * 4)
    user = f"Write {n} questions.\n\nSOURCE TEXT:\n{excerpt}"

    client = create_llm_client(key, timeout=QuizConfig.LLM_TIMEOUT_SECONDS, max_retries=0)
    finish, text = None, ""
    for attempt in range(2):
        try:
            limiter.acquire_sync(estimate_tokens(_QUIZ_SYSTEM) + estimate_tokens(user), QuizConfig.LLM_MAX_TOKENS)
            resp = client.chat.completions.create(
                model=QuizConfig.LLM_MODEL,
                messages=[{"role": "system", "content": _QUIZ_SYSTEM}, {"role": "user", "content": user}],
                # Accepted by OpenAI only with reasoning_effort "none".
                temperature=0.3,
                reasoning_effort=NON_RAG_REASONING_EFFORT,
                response_format={"type": "json_object"},
                **{COMPLETION_TOKEN_PARAMETER: QuizConfig.LLM_MAX_TOKENS},
            )
            finish = resp.choices[0].finish_reason
            text = (resp.choices[0].message.content or "").strip()
            data = json.loads(text)
            break
        except RATE_LIMIT_ERRORS as e:
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

    items = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        body = re.sub(r"\s+", " ", text)
        logger.warning("document_quiz_unexpected_shape step=document_quiz finish_reason=%s body=%s",
                       finish, body[:500] + ("\u2026" if len(body) > 500 else ""))
        return []

    units = _evidence_units(excerpt)
    grounded = [it for it in items if isinstance(it, dict) and evidence_supported(it.get("evidence"), units)]
    if len(grounded) < len(items):
        logger.warning("document_quiz_evidence_dropped kept=%d of=%d", len(grounded), len(items))
    kept, dropped, flags = ns.validate_quiz(grounded, quiz_id, 0, 0, max_questions=n)
    logger.info("document_quiz file=%s asked=%d grounded=%d kept=%d flags=%s", filename, len(items),
                len(grounded), len(kept or []), {k: v for k, v in flags.items() if v})
    if not kept:
        logger.warning("document_quiz_failed reason=too_few_valid_questions finish_reason=%s", finish)
        return []
    return [{k: q[k] for k in _QUIZ_FIELDS} for q in kept]


# topic_id -> {fingerprint, topic}. Keyed per topic, so new uploads only invalidate topics whose
# concepts changed.
_TOPIC_CACHE: dict[str, dict] = {}
_GENERATING: set[str] = set()
_TASKS: set[asyncio.Task] = set()   # keeps background tasks alive until done


async def _generate_topic(topic_id: str, week: str, members: list[dict],
                           names: list[str], fingerprint: str, filename: str,
                           title: Optional[str] = None) -> None:
    try:
        pool = await asyncio.to_thread(_questions_from_document, filename, topic_id,
                                       QuizConfig.POOL_SIZE_PER_TOPIC)

        sources = sorted({c.get("source", "") for c in members if c.get("source")})
        topic = {
            "id": topic_id,
            "day": _week_num(week),
            "name": title or (f"{week} — {names[0]}" if names else week),
            "desc": f"{len(names)} concepts from {', '.join(sources) or 'your uploads'}.",
            # Too few usable questions counts as a failed call, so the reader is offered a retry.
            "status": "ready" if len(pool) >= QuizConfig.MIN_POOL_TO_SHOW_TOPIC else "failed",
            "questions": pool,
        }
        _TOPIC_CACHE[topic_id] = {"fingerprint": fingerprint, "topic": topic}
    finally:
        _GENERATING.discard(topic_id)


def _user_documents(user_id) -> list:
    """The user's document rows, oldest week first. A seam for tests."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, filename, week, status FROM documents WHERE user_id = ? "
            "ORDER BY uploaded_at", (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# A title line: a short run of words, not a page number, URL, date or copyright notice.
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
    """The document's title from its first dozen lines: a Lecture/Chapter/Week line (joined to
    the next if only a label), else the first title-like line."""
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
    """What the Quiz tab calls a document: its title line if the text has one, else the cleaned
    filename."""
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
    """One document's quiz entry and state; generates nothing. A concept taught in two documents
    is in both quizzes."""
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
        # Stored as generated, shuffled on the way out. pass_marks covers every difficulty filter,
        # so the client never computes one.
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
    """Every document with its quiz state. Generation waits for POST
    /api/quiz/documents/{id}/generate."""
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
    """Starts a document's quiz in the background unless cached or running. Poll GET /api/quiz
    until status leaves 'generating'."""
    doc = next((d for d in _user_documents(user["id"]) if d["id"] == doc_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="That document is not in your library.")

    concepts, _edges = _graph()
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
            topic_id, members[0].get("week") or "Week 1", members, names,
            _topic_fingerprint(names), doc["filename"], title=entry["title"],
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


# --- results: every attempt per user in one JSON file; summaries are computed on read ---

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
    """Adds an attempt and returns the user's attempts at that quiz. Re-read under the lock so
    concurrent saves both survive."""
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
    """One quiz's attempts as the Quiz tab lists them: best share correct (later wins ties) and
    the latest. Old 'points' fields are ignored."""
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
    """Quiz ids of that kind the user has passed; notes tick sections from it across restarts."""
    return {a["quiz_id"] for a in _load_results().get(str(user_id), [])
            if a.get("kind") == kind and a.get("passed")}


# --- section quizzes: kept per user so the Quiz tab lists them after the in-memory notes are gone ---

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
    """Records a section quiz for a user, skipping unchanged writes. Never raises: a failed
    record must not fail the section."""
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
    """The user's section quizzes by note title in section order, from SECTION_QUIZ_FILE so they
    survive a restart."""
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
