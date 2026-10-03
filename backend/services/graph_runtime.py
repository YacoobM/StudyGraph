import asyncio
import json
import asyncio
import time
from datetime import datetime

from app.config import COMPLETION_TOKEN_PARAMETER, NON_RAG_REASONING_EFFORT, OUTPUT_DIR
from services.llm_gateway import estimate_tokens, graph_limiter
from app.logging_config import get_logger

logger = get_logger(__name__)


def save_seed_entities(seed_entities):
    try:
        output_file = OUTPUT_DIR / "graph_entities.json"
        data = {
            "timestamp": datetime.now().isoformat(),
            "count": len(seed_entities),
            "seed_entities": seed_entities,
        }
        with open(output_file, "w") as file:
            json.dump(data, file, indent=2)
        return str(output_file)
    except Exception as error:
        logger.exception("seed_entities_save_failed")
        return None


def save_extraction_result(timings, nodes, edges):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = OUTPUT_DIR / f"result_{timestamp}.json"
        data = {
            "timestamp": timestamp,
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "timings": timings,
            },
        }
        with open(output_file, "w") as file:
            json.dump(data, file, indent=2)
        return str(output_file)
    except Exception as error:
        logger.exception("extraction_result_save_failed")
        return None


def save_graph_json(nodes, edges, fusion_stats=None, graph_name=""):
    try:
        output_file = OUTPUT_DIR / "graph_json.json"
        data = {
            "timestamp": datetime.now().isoformat(),
            "name": graph_name,
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "fusion_stats": fusion_stats or {},
            },
        }
        with open(output_file, "w") as file:
            json.dump(data, file, indent=2)
        return str(output_file)
    except Exception as error:
        logger.exception("graph_json_save_failed")
        return None


def get_collection(chroma, project_id, user_id="default"):
    return chroma.get_or_create_collection(name=f"user_{user_id}_project_{project_id}")


async def call_llm(client, model, prompt, text_batch, max_tokens=1000):
    request_started = time.perf_counter()
    estimated_input = estimate_tokens(prompt) + estimate_tokens(text_batch)
    logger.info(
        "llm_request_started model=%s estimated_input_tokens=%d max_output_tokens=%d",
        model,
        estimated_input,
        max_tokens,
    )
    try:
        await graph_limiter.acquire(
            estimated_input,
            max_tokens,
        )
        request = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_batch},
            ],
            "model": model,
            "reasoning_effort": NON_RAG_REASONING_EFFORT,
            COMPLETION_TOKEN_PARAMETER: max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = await asyncio.to_thread(
            client.chat.completions.create,
            **request,
        )
        usage = getattr(response, "usage", None)
        choice = response.choices[0] if getattr(response, "choices", None) else None
        logger.info(
            "llm_request_succeeded model=%s duration_ms=%.1f prompt_tokens=%s completion_tokens=%s total_tokens=%s finish_reason=%s",
            model,
            (time.perf_counter() - request_started) * 1000,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "total_tokens", None),
            getattr(choice, "finish_reason", None),
        )
        return response
    except Exception as error:
        logger.exception(
            "llm_call_failed model=%s duration_ms=%.1f error_type=%s status_code=%s error=%s headers=%s",
            model,
            (time.perf_counter() - request_started) * 1000,
            type(error).__name__,
            getattr(error, "status_code", None),
            str(error),
            getattr(getattr(error, "response", None), "headers", None),
        )
        raise
