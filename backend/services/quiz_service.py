import random
import uuid

OPTION_COUNT = 4
DISTRACTOR_COUNT = OPTION_COUNT - 1


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