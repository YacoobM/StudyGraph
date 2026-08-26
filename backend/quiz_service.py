"""
Quiz generation from the extracted concept graph.

    GET  /api/quiz            topics + quizzes built from what was uploaded
    POST /api/quiz/results    record an attempt

Two generation paths:

  * With GROQ_API_KEY set, an LLM writes the questions from the concepts and
    relations found in the document. Better prose, real distractors.
  * Without a key, questions are built directly from the graph's relations by
    template. Lower quality, but still genuinely derived from the uploaded
    document rather than canned — and it needs no key and costs nothing.

Either way the questions come from the user's own material. If nothing has
been uploaded the route returns empty lists, and the UI says so rather than
showing samples.

Mount alongside the other routers:

    from quiz_service import router as quiz_router, set_graph_source
    app.include_router(quiz_router)
    set_graph_source(lambda: engine)
"""

from __future__ import annotations

import json
import os
import random
import re
from collections import defaultdict
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_documents import current_user

router = APIRouter(tags=["quiz"])

PTS = {"easy": 10, "medium": 20, "hard": 30}
QUESTIONS_PER_QUIZ = 3
MAX_QUIZZES_PER_TOPIC = 3

# The app object registers a callable returning the live engine, so this module
# never imports main.py (which would be circular).
_graph_source: Optional[Callable] = None


def set_graph_source(fn: Callable) -> None:
    global _graph_source
    _graph_source = fn


def _graph():
    """Returns (concepts, edges) or ([], []) when nothing is loaded."""
    if _graph_source is None:
        return [], []
    engine = _graph_source()
    if engine is None:
        return [], []
    return list(getattr(engine, "concepts", []) or []), list(getattr(engine, "edges", []) or [])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _week_num(week: str) -> int:
    digits = re.sub(r"\D+", "", str(week or ""))
    return int(digits) if digits else 1


def _distractors(answer: str, pool: list[str], n: int = 3,
                 exclude: set[str] | None = None) -> list[str]:
    """
    Wrong options drawn from other real concepts, never invented.

    `exclude` drops anything already named in the question stem — otherwise a
    concept mentioned in the question turns up as an option, which either gives
    the answer away or reads as a mistake.
    """
    blocked = {answer} | (exclude or set())
    others = [c for c in pool if c not in blocked]
    random.shuffle(others)
    return others[:n]


def _difficulty(degree: int) -> str:
    if degree >= 4:
        return "easy"        # a hub concept is hard to miss
    if degree >= 2:
        return "medium"
    return "hard"


def _mcq(question: str, answer: str, pool: list[str], why: str, diff: str,
         exclude: set[str] | None = None) -> Optional[dict]:
    wrong = _distractors(answer, pool, exclude=exclude)
    if len(wrong) < 2:
        return None          # not enough material for a fair question
    options = wrong + [answer]
    random.shuffle(options)
    return {
        "q": question,
        "o": options,
        "a": options.index(answer),
        "why": why,
        "d": diff,
    }


# ---------------------------------------------------------------------------
# template generation — no API key needed
# ---------------------------------------------------------------------------

def _questions_from_graph(concepts: list[dict], edges: list[dict], week: str) -> list[dict]:
    names = sorted({c["name"] for c in concepts if c.get("week") == week})
    if len(names) < 4:
        names = sorted({c["name"] for c in concepts})   # widen if the week is thin
    if len(names) < 4:
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

        q = _mcq(
            f"According to your sources, what does {src} {rel}?",
            tgt, names,
            f"Your material records: {src} {rel} {tgt}.",
            _difficulty(degree[tgt]),
            exclude={src},
        )
        if q:
            out.append(q)

        q2 = _mcq(
            f"Which concept {rel} {tgt}?",
            src, names,
            f"Your material records: {src} {rel} {tgt}.",
            _difficulty(degree[src]),
            exclude={tgt},
        )
        if q2:
            out.append(q2)

    # one question about the most connected concept in the topic
    if degree:
        hub = max(names, key=lambda n: degree[n], default=None)
        if hub:
            q3 = _mcq(
                "Which concept is the most connected in this part of your material?",
                hub, names,
                f"{hub} appears in {degree[hub]} of the relationships extracted from your document.",
                "medium",
            )
            if q3:
                out.append(q3)

    random.shuffle(out)
    return out


# ---------------------------------------------------------------------------
# LLM generation — used when GROQ_API_KEY is present
# ---------------------------------------------------------------------------

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
        f"- {e['from']} {e.get('relation', 'relates to')} {e['to']}" for e in edges[:40]
    )
    prompt = LLM_PROMPT.format(
        concepts=", ".join(concepts[:40]), relations=relations, n=n,
    )

    try:
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            # the questions are data, not deliberation, so keep reasoning cheap
            reasoning_effort="low",
        )
        text = resp.choices[0].message.content.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        items = json.loads(text)
    except Exception:
        return []        # any failure falls back to template generation

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


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@router.get("/api/quiz")
async def get_quiz(user=Depends(current_user)):
    """Topics and quizzes generated from whatever has been uploaded."""
    concepts, edges = _graph()

    if not concepts:
        return {
            "topics": [], "quizzes": [], "generated": False,
            "reason": "No documents have been processed yet.",
        }

    by_week: dict[str, list[dict]] = defaultdict(list)
    for c in concepts:
        by_week[c.get("week") or "Week 1"].append(c)

    topics, quizzes = [], []
    used_llm = False

    for week in sorted(by_week, key=_week_num):
        members = by_week[week]
        names = sorted({c["name"] for c in members})
        if len(names) < 4:
            continue      # too thin to ask a fair multiple-choice question

        topic_id = f"w{_week_num(week)}"
        sources = sorted({c.get("source", "") for c in members if c.get("source")})
        topics.append({
            "id": topic_id,
            "day": _week_num(week),
            "name": f"{week} — {names[0]}" if names else week,
            "desc": f"{len(names)} concepts from {', '.join(sources) or 'your uploads'}.",
        })

        week_edges = [e for e in edges if e["from"] in names and e["to"] in names] or edges
        pool = _questions_from_llm(names, week_edges, QUESTIONS_PER_QUIZ * MAX_QUIZZES_PER_TOPIC)
        if pool:
            used_llm = True
        else:
            pool = _questions_from_graph(members, edges, week)

        for i in range(0, min(len(pool), QUESTIONS_PER_QUIZ * MAX_QUIZZES_PER_TOPIC), QUESTIONS_PER_QUIZ):
            chunk = pool[i:i + QUESTIONS_PER_QUIZ]
            if len(chunk) < 2:
                break
            hardest = max(chunk, key=lambda q: PTS.get(q["d"], 20))["d"]
            quizzes.append({
                "id": f"{topic_id}-{i // QUESTIONS_PER_QUIZ + 1}",
                "topic": topic_id,
                "title": f"Set {i // QUESTIONS_PER_QUIZ + 1}",
                "difficulty": hardest,
                "questions": chunk,
            })

    return {
        "topics": topics,
        "quizzes": quizzes,
        "generated": True,
        "source": "llm" if used_llm else "graph",
        "reason": None if quizzes else
                  "Not enough concepts yet — upload more material for questions.",
    }


class QuizResult(BaseModel):
    quiz_id: str
    correct: int
    total: int
    points: int


@router.post("/api/quiz/results")
async def save_result(body: QuizResult, user=Depends(current_user)):
    """Records an attempt. Best score wins, matching the UI's rule."""
    if body.total <= 0:
        raise HTTPException(status_code=422, detail="A quiz needs at least one question.")

    passed = body.correct / body.total >= 2 / 3
    _RESULTS.setdefault(user["id"], {})
    prev = _RESULTS[user["id"]].get(body.quiz_id)
    if prev is None or body.points > prev["points"]:
        _RESULTS[user["id"]][body.quiz_id] = {
            "correct": body.correct, "total": body.total,
            "points": body.points, "passed": passed,
        }
    return {"result": _RESULTS[user["id"]][body.quiz_id], "passed": passed}


@router.get("/api/quiz/results")
async def list_results(user=Depends(current_user)):
    return {"results": _RESULTS.get(user["id"], {})}


# Attempts are kept in memory: they are cheap to redo and the generated quiz
# changes whenever a new document is uploaded, so persisting them would leave
# results pointing at questions that no longer exist.
_RESULTS: dict[int, dict] = {}
