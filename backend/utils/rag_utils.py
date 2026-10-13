"""
RAG utilities for prompt construction and LLM calls.
"""

from app.logging_config import get_logger
from app.config import (
    RAG_REASONING_EFFORT,
)
from services.llm_gateway import estimate_tokens, rag_limiter

import json

logger = get_logger(__name__)


def prioritize_graph(retrieved, sim_threshold=0.4):
    """
    Decide if graph should be prioritized in the response.
    Returns True if retrieval is weak (low similarity scores).
    """
    if not retrieved:
        return True
    avg_similarity = sum(r["similarity"] for r in retrieved) / len(retrieved)
    return avg_similarity < sim_threshold


def filter_sources_by_chunks_used(sources, used_chunk_numbers):
    """Return only sources that were actually cited by the LLM."""
    return [sources[i - 1] for i in used_chunk_numbers if 0 < i <= len(sources)]


def build_rag_system_prompt(prioritize_graph=False):
    """
    Build the system prompt for RAG queries.

    Args:
        graph_context_avvailable: Whether graph context will be used in the user prompt

    Returns:
        str: System prompt text
    """
    base = """You are a concise study assistant. Answer the specific question directly,
    using only the supplied document text and relevant knowledge-graph context.

    - Put the direct answer in the first sentence.
    - Include only details that help answer the question.
    - Do not add related topics, background, or speculation unless asked.
    - If the evidence supports only part of the question, answer that part, then state
    the limitation briefly.
    - If the evidence does not answer the question, say so briefly.

    CITATION RULES:
    - Cite document facts with page numbers immediately after the claim.
    - Format: [p. 5] for a single page and [pp. 3-5] for a range.
    - Do not present unsupported claims as facts.
    """

    if prioritize_graph:
        base += "\n -For broad/vague/bigger picture questions or when text is limited, use the knowledge graph to identify key concepts and relationships"

    return base


def build_rag_user_prompt(context_text, query, graph_section=""):
    """
    Build the user prompt for RAG queries.

    Args:
        graph_context_avvailable: Whether graph context will be used in the user prompt

    Returns:
        str: user prompt text
    """
    graph_part = (
        f"\n\nKNOWLEDGE GRAPH CONTEXT: \n{graph_section}" if graph_section else ""
    )

    return f"""TEXT FROM DOCUMENTS (cite these):
                {context_text}

                {graph_part}

                              QUESTION: {query}

    Answer this exact question, not just its general topic.
    Start with the direct answer and include only relevant supporting details.
    Use graph context only when it directly helps answer the question; do not list
    unrelated concepts or connections.
    If only part of the question is supported, answer that part and state the
    limitation in one brief sentence. Do not guess.
    Cite document facts using the page numbers shown in the text chunks.

    Respond with ONLY a JSON object (no markdown, no explanation):
    {{
    "answer": "Your answer here",
    "chunks_used": [1, 3, 5],
    "concepts_mentioned": ["concept1", "concept2"]
    }}"""


def call_rag_llm(
    client, model, system_prompt, user_prompt, reasoning_effort=RAG_REASONING_EFFORT
):
    """
    Make the LLM call for RAG queries.

    Args:
        client: OpenAI client
        model: Model name
        system_prompt: System prompt text
        user_prompt: user prompt text
        reasoning_effort: Reasoning effort level (ideally medium++)

    Returns:
        dict: LLM resposne text and chunks used

    Raises:
        AttributeError, KeyError, ValueError: Response parsing errors
        Exception: API or other errors
    """
    estimated_input = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)

    rag_limiter.acquire_sync(
        estimated_input,
        2000,
    )

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=model,
        reasoning_effort=reasoning_effort,
    )

    response_text = response.choices[0].message.content
    response_json = json.loads(response_text)

    return {
        "answer": response_json.get("answer", ""),
        "chunks_used": response_json.get("chunks_used", []),
        "concepts_mentioned": response_json.get("concepts_mentioned", []),
    }
