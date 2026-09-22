"""
Quiz generation from the extracted concept graph.

    GET  /api/quiz            topics + question pools built from what was uploaded
    POST /api/quiz/results    record a topic attempt (best score wins)

With OPENAI_API_KEY set, an LLM writes the questions. Without one, questions
are built from the graph's relations by template.

Each topic is cached and generated independently, keyed by a fingerprint of
its own concepts. Uploading new material only invalidates the topics whose
concepts changed — other topics keep serving from cache while a newly
changed topic generates in the background and reports status "generating"
until ready.

Mount:
    from services.quiz_service import router as quiz_router, set_graph_source
    app.include_router(quiz_router)
    set_graph_source(lambda: engine)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
from collections import defaultdict
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import LLM_API_KEY_ENV, EXTRACTION_MODEL, NON_RAG_REASONING_EFFORT
from api.routes.auth import current_user
from models.quiz_models import TopicResult

router = APIRouter(tags=["quiz"])


class QuizConfig:
    POOL_SIZE_PER_TOPIC = 12
    MIN_CONCEPTS_PER_TOPIC = 4
    MIN_POOL_TO_SHOW_TOPIC = 2
    MASTERY_THRESHOLD = 2 / 3
    DISTRACTOR_COUNT = 3
    HUB_DEGREE_EASY = 4
    HUB_DEGREE_MEDIUM = 2
    LLM_TIMEOUT_SECONDS = 45
    LLM_MODEL = EXTRACTION_MODEL
    LLM_MAX_CONCEPTS = 40
    LLM_MAX_RELATIONS = 40
    POLL_HINT_SECONDS = 4


PTS = {"easy": 10, "medium": 20, "hard": 30}

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


def _distractors(answer: str, pool: list[str], exclude: set[str] | None = None) -> list[str]:
    blocked = {answer} | (exclude or set())
    others = [c for c in pool if c not in blocked]
    random.shuffle(others)
    return others[:QuizConfig.DISTRACTOR_COUNT]


def _difficulty(degree: int) -> str:
    if degree >= QuizConfig.HUB_DEGREE_EASY:
        return "easy"
    if degree >= QuizConfig.HUB_DEGREE_MEDIUM:
        return "medium"
    return "hard"


def _mcq(question: str, answer: str, pool: list[str], why: str, diff: str,
         exclude: set[str] | None = None) -> Optional[dict]:
    wrong = _distractors(answer, pool, exclude)
    if len(wrong) < 2:
        return None
    options = wrong + [answer]
    random.shuffle(options)
    return {"q": question, "o": options, "a": options.index(answer), "why": why, "d": diff}


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


# topic_id -> {"fingerprint": str, "topic": dict}. Keyed per topic, not per
# whole-graph size, so uploading new material only invalidates the topics
# whose own concepts changed.
_TOPIC_CACHE: dict[str, dict] = {}
_GENERATING: set[str] = set()
_TASKS: set[asyncio.Task] = set()   # keeps background tasks alive until done


async def _generate_topic(topic_id: str, week: str, members: list[dict],
                           edges: list[dict], names: list[str], fingerprint: str) -> None:
    try:
        week_edges = [e for e in edges if e["from"] in names and e["to"] in names] or edges
        pool = await asyncio.to_thread(_questions_from_llm, names, week_edges, QuizConfig.POOL_SIZE_PER_TOPIC)
        if not pool:
            pool = _questions_from_graph(members, edges, week)
        for q in pool:
            q["id"] = _question_id(topic_id, q["q"])

        sources = sorted({c.get("source", "") for c in members if c.get("source")})
        topic = {
            "id": topic_id,
            "day": _week_num(week),
            "name": f"{week} — {names[0]}" if names else week,
            "desc": f"{len(names)} concepts from {', '.join(sources) or 'your uploads'}.",
            "status": "ready" if len(pool) >= QuizConfig.MIN_POOL_TO_SHOW_TOPIC else "empty",
            "questions": pool,
        }
        _TOPIC_CACHE[topic_id] = {"fingerprint": fingerprint, "topic": topic}
    finally:
        _GENERATING.discard(topic_id)


@router.get("/api/quiz")
async def get_quiz(user=Depends(current_user)):
    concepts, edges = _graph()
    if not concepts:
        return {"topics": [], "generated": False, "reason": "No documents have been processed yet."}

    by_week: dict[str, list[dict]] = defaultdict(list)
    for c in concepts:
        by_week[c.get("week") or "Week 1"].append(c)

    topics = []
    for week in sorted(by_week, key=_week_num):
        members = by_week[week]
        names = sorted({c["name"] for c in members})
        if len(names) < QuizConfig.MIN_CONCEPTS_PER_TOPIC:
            continue

        week_num = _week_num(week)
        topic_id = f"w{week_num}"
        fingerprint = _topic_fingerprint(names)

        cached = _TOPIC_CACHE.get(topic_id)
        if cached and cached["fingerprint"] == fingerprint:
            topics.append(cached["topic"])
            continue

        sources = sorted({c.get("source", "") for c in members if c.get("source")})
        topics.append({
            "id": topic_id,
            "day": week_num,
            "name": f"{week} — {names[0]}",
            "desc": f"{len(names)} concepts from {', '.join(sources) or 'your uploads'}.",
            "status": "generating",
            "questions": [],
        })

        if topic_id not in _GENERATING:
            _GENERATING.add(topic_id)
            task = asyncio.create_task(_generate_topic(topic_id, week, members, edges, names, fingerprint))
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)

    return {
        "topics": topics,
        "generated": bool(topics),
        "reason": None if topics else "Not enough concepts yet — upload more material for questions.",
        "poll_after_seconds": QuizConfig.POLL_HINT_SECONDS,
    }


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


@router.post("/api/quiz/results")
async def save_result(body: TopicResult, user=Depends(current_user)):
    if body.total <= 0:
        raise HTTPException(status_code=422, detail="A quiz needs at least one question.")

    passed = body.correct / body.total >= QuizConfig.MASTERY_THRESHOLD
    _RESULTS.setdefault(user["id"], {})
    prev = _RESULTS[user["id"]].get(body.topic_id)
    if prev is None or body.points > prev["points"]:
        _RESULTS[user["id"]][body.topic_id] = {
            "correct": body.correct, "total": body.total, "points": body.points, "passed": passed,
        }
    return {"result": _RESULTS[user["id"]][body.topic_id], "passed": passed}


@router.get("/api/quiz/results")
async def list_results(user=Depends(current_user)):
    return {"results": _RESULTS.get(user["id"], {})}


_RESULTS: dict[int, dict] = {}
