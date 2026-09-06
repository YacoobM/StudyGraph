import json
import logging
from datetime import datetime

from app.config import OUTPUT_DIR

logger = logging.getLogger(__name__)


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


def save_graph_json(nodes, edges, fusion_stats=None):
    try:
        output_file = OUTPUT_DIR / "graph_json.json"
        data = {
            "timestamp": datetime.now().isoformat(),
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


def call_llm(client, model, prompt, text_batch):
    try:
        return client.chat.completions.create(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_batch},
            ],
            model=model,
            reasoning_effort="none",
            response_format={"type": "json_object"},
        )
    except Exception as error:
        logger.exception("llm_call_failed model=%s", model)
        raise