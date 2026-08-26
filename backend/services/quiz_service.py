import json
import random
import uuid

from evolution_data import _call_with_retry, _batches, BATCH_TOKEN_OVERHEAD

OPTION_COUNT = 4
DISTRACTOR_COUNT = OPTION_COUNT - 1

AI_QUESTIONS_PER_BATCH = 5
AI_QUESTION_TOKENS_PER_ITEM = 220

_AI_QUIZ_SYSTEM_PROMPT = (
    "You are a study assistant generating multiple-choice quiz questions from "
    "course material. For each excerpt below, write one question that tests "
    "real understanding of the material, not just wording recall. Give 4 "
    "options with exactly one correct answer, plus a one-sentence explanation "
    "grounded in the excerpt. Use the page number in the excerpt's [brackets] "
    "as the citation, if present.\n"
    'Respond with JSON: {"items": [{"key": "<the exact KEY given>", '
    '"question": "...", "options": ["...", "...", "...", "..."], '
    '"correct_index": 0, "explanation": "...", "citation": "..."}]}, '
    "one entry per excerpt, in the same order."
)


def _working_edges(engine):
    """
    Return the edge list to quiz from.

    Uses fused_edges if fusion has run, otherwise raw edges.
    """
    return engine.fused_edges if engine.fused_edges else engine.edges


def _relation_pool(edges):
    """
    Return every distinct relation label present in `edges`.

    Used as the distractor pool. No fixed vocabulary — works with any
    relation label the graph actually contains, extraction-based or
    fusion-generated.
    """
    return sorted({e["relation"] for e in edges if e.get("relation")})


def build_relationship_question(edge, relation_pool):
    """
    Build one multiple-choice question from a single edge.

    Returns None if there aren't enough distinct relation labels in
    `relation_pool` to build distractors (needs at least DISTRACTOR_COUNT
    other labels besides the correct one).
    """
    correct = edge["relation"]
    other_relations = [r for r in relation_pool if r != correct]
    if len(other_relations) < DISTRACTOR_COUNT:
        return None

    distractors = random.sample(other_relations, DISTRACTOR_COUNT)
    options = distractors + [correct]
    random.shuffle(options)

    return {
        "id": str(uuid.uuid4()),
        "type": "relationship",
        "question": f"What is the relationship from '{edge['from']}' to '{edge['to']}'?",
        "options": options,
        "correct_index": options.index(correct),
        "explanation": edge.get("summary") or edge.get("context") or "",
        "source": edge.get("source", "unknown"),
    }


def generate_relationship_quiz(engine, count=5):
    """
    Generate up to `count` relationship questions from the current graph.

    Deterministic and local — no LLM call. Question text, correct answer,
    and distractors are all built from engine.edges / engine.fused_edges,
    so this works immediately after upload and does not depend on fusion
    having completed.
    """
    edges = [e for e in _working_edges(engine) if e.get("relation")]
    if not edges:
        return []

    relation_pool = _relation_pool(edges)
    sample_edges = random.sample(edges, min(count, len(edges)))

    questions = []
    for edge in sample_edges:
        q = build_relationship_question(edge, relation_pool)
        if q:
            questions.append(q)
    return questions


def _sample_chunks(engine, count):
    """
    Pick up to `count` chunks from across all weeks, for AI quiz grounding.

    Notes: Pulls from engine.chunks_by_week, the same store the concept
    evolution feature reads from. Skips weeks with no chunks.
    """
    pool = []
    for chunks in engine.chunks_by_week.values():
        pool.extend(chunks)
    if not pool:
        return []
    return random.sample(pool, min(count, len(pool)))


async def generate_ai_quiz(client, engine, count=5):
    """
    Generate up to `count` AI-written questions grounded in raw chunk text.

    Notes: Costs one or more Groq calls (batched, AI_QUESTIONS_PER_BATCH
    per call). Works even if extraction/fusion missed a concept, since it
    reads straight from the chunk store rather than the graph.
    """
    chunks = _sample_chunks(engine, count)
    if not chunks:
        return []

    questions = []
    batches = list(_batches(chunks, AI_QUESTIONS_PER_BATCH))

    for batch in batches:
        sections = []
        for i, chunk in enumerate(batch):
            citation = engine.format_citation(chunk.get("pages", []))
            header = f"[{citation}]" if citation else "[unknown page]"
            sections.append(f'KEY: q{i}\n{header}\n{chunk["text"][:800]}')
        user_prompt = "\n\n---\n\n".join(sections)

        max_tokens = BATCH_TOKEN_OVERHEAD + AI_QUESTION_TOKENS_PER_ITEM * len(batch)
        raw = await _call_with_retry(
            client, _AI_QUIZ_SYSTEM_PROMPT, user_prompt,
            json_mode=True, temperature=0.4, max_tokens=max_tokens,
        )
        if not raw:
            continue

        try:
            parsed = json.loads(raw)
            items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            options = item.get("options")
            correct_index = item.get("correct_index")
            question = item.get("question")
            if not (isinstance(options, list) and len(options) == OPTION_COUNT
                    and isinstance(correct_index, int) and 0 <= correct_index < OPTION_COUNT
                    and isinstance(question, str) and question.strip()):
                continue
            questions.append({
                "id": str(uuid.uuid4()),
                "type": "ai",
                "question": question,
                "options": options,
                "correct_index": correct_index,
                "explanation": item.get("explanation", ""),
                "citation": item.get("citation", ""),
            })

    return questions


def score_submission(questions, answers):
    """
    Score a submitted set of answers against the original questions.

    `answers`: dict of question_id -> selected_index.
    Returns (correct_count, results), where results is a list of per-question
    outcomes with the explanation attached, so the frontend can show feedback
    without a second lookup.
    """
    by_id = {q["id"]: q for q in questions}
    results = []
    correct_count = 0

    for question_id, selected_index in answers.items():
        q = by_id.get(question_id)
        if q is None:
            continue
        is_correct = selected_index == q["correct_index"]
        correct_count += int(is_correct)
        results.append({
            "question_id": question_id,
            "correct": is_correct,
            "correct_index": q["correct_index"],
            "explanation": q["explanation"],
        })

    return correct_count, results