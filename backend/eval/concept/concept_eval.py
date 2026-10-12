"""Evaluate StudyGraph concept and relationship extraction against JSON labels.

Run from the repository root with:
    cd backend
    python -m eval.concept.concept_eval

The evaluator uses StudyGraph's document parser, graph-engine chunker, and
extraction path. Chunks are indexed only in a temporary in-memory collection;
the evaluator does not fuse or persist anything to a user graph.
"""

import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from chromadb import EphemeralClient

from app.config import EXTRACTION_MODEL, OPENAI_API_KEY
from app.logging_config import configure_logging, get_logger
from schemas.extraction import ExtractionResult
from services.document_service import extract_text, extract_with_exp_backoff, make_extraction_batches
from services.graph_engine import StudyGraphEngine
from services.llm_provider import create_llm_client

EVAL_DIR = Path(__file__).resolve().parent
DOCS_DIR = EVAL_DIR / "source_docs" / "docs"
LABELS_DIR = EVAL_DIR / "gold_labels"
REPORTS_DIR = EVAL_DIR / "reports"
logger = get_logger(__name__)


def normalize_name(value: str) -> str:
    """Normalize case and punctuation while retaining meaningful symbols."""
    value = value.casefold().replace("_", " ")
    return " ".join(re.sub(r"[^\w\s-]", " ", value).split())


def load_gold_labels(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        labels = json.load(handle)
    if not isinstance(labels.get("concepts"), list) or not isinstance(
        labels.get("relationships"), list
    ):
        raise ValueError(f"Expected concepts and relationships arrays in {path}")
    return labels


def gold_concept_names(labels: dict) -> dict[str, dict]:
    concepts = {}
    for concept in labels["concepts"]:
        concept_id = concept.get("id")
        name = concept.get("name")
        if not concept_id or not isinstance(name, str) or not name.strip():
            raise ValueError("Every gold concept needs a non-empty id and name")
        concepts[concept_id] = concept
    return concepts


def gold_relationships(labels: dict) -> list[dict]:
    concepts = gold_concept_names(labels)
    relationships = []
    for relationship in labels["relationships"]:
        source = concepts.get(relationship.get("from"))
        target = concepts.get(relationship.get("to"))
        relation = relationship.get("relation")
        if source is None or target is None or not isinstance(relation, str):
            raise ValueError("Gold relationship has an unknown concept id or relation")
        relationships.append(
            {
                "from": source["name"],
                "to": target["name"],
                "relation": relation,
                "aliases_from": source.get("aliases") or [],
                "aliases_to": target.get("aliases") or [],
            }
        )
    return relationships


def metric_counts(true_positive: int, predicted: int, gold: int) -> dict:
    precision = true_positive / predicted if predicted else 0.0
    recall = true_positive / gold if gold else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "true_positive": true_positive,
        "false_positive": predicted - true_positive,
        "false_negative": gold - true_positive,
        "precision": precision,
        "recall": recall,
        "f1_score": f1,
    }


def maximum_matching(predictions: list, gold_items: list, compatible) -> tuple[set, set]:
    """Return matched prediction and gold indices using maximum bipartite matching."""
    gold_to_prediction = {}

    def assign(prediction_index: int, seen: set[int]) -> bool:
        for gold_index, gold_item in enumerate(gold_items):
            if gold_index in seen or not compatible(
                predictions[prediction_index], gold_item
            ):
                continue
            seen.add(gold_index)
            previous = gold_to_prediction.get(gold_index)
            if previous is None or assign(previous, seen):
                gold_to_prediction[gold_index] = prediction_index
                return True
        return False

    for prediction_index in range(len(predictions)):
        assign(prediction_index, set())
    matched_gold = set(gold_to_prediction)
    return set(gold_to_prediction.values()), matched_gold


def concept_options(concept: dict) -> set[str]:
    return {
        normalize_name(value)
        for value in [concept["name"], *(concept.get("aliases") or [])]
        if isinstance(value, str) and value.strip()
    }


def relationship_options(relationship: dict) -> set[tuple[str, str, str]]:
    return {
        (
            normalize_name(source),
            normalize_name(relationship["relation"]),
            normalize_name(target),
        )
        for source in [relationship["from"], *relationship["aliases_from"]]
        for target in [relationship["to"], *relationship["aliases_to"]]
        if isinstance(source, str) and isinstance(target, str)
    }


def evaluate_concepts(predictions: list[dict], gold: list[dict]) -> dict:
    predicted_names = sorted(
        {normalize_name(item["name"]) for item in predictions if item.get("name")}
    )
    gold_names = sorted({normalize_name(item["name"]) for item in gold})
    gold_options = [concept_options(item) for item in gold]

    strict_predicted, _ = maximum_matching(
        predicted_names, gold_names, lambda predicted, expected: predicted == expected
    )
    alias_predicted, alias_gold = maximum_matching(
        predicted_names,
        gold_options,
        lambda predicted, expected: predicted in expected,
    )
    return {
        "strict": metric_counts(len(strict_predicted), len(predicted_names), len(gold_names)),
        "alias_aware": metric_counts(
            len(alias_predicted), len(predicted_names), len(gold_options)
        ),
        "unmatched_predictions": [
            predicted_names[index]
            for index in range(len(predicted_names))
            if index not in alias_predicted
        ],
        "unmatched_gold": [
            gold[index]["name"]
            for index in range(len(gold))
            if index not in alias_gold
        ],
    }


def evaluate_relationships(predictions: list[dict], gold: list[dict]) -> dict:
    predicted = sorted(
        {
            (
                normalize_name(item["from"]),
                normalize_name(item["relation"]),
                normalize_name(item["to"]),
            )
            for item in predictions
            if item.get("from") and item.get("to") and item.get("relation")
        }
    )
    strict_gold = [
        {
            "options": {
                (
                    normalize_name(item["from"]),
                    normalize_name(item["relation"]),
                    normalize_name(item["to"]),
                )
            }
        }
        for item in gold
    ]
    alias_gold = [{"options": relationship_options(item)} for item in gold]
    strict_predicted, _ = maximum_matching(
        predicted,
        strict_gold,
        lambda prediction, expected: prediction in expected["options"],
    )
    alias_predicted, alias_matches = maximum_matching(
        predicted,
        alias_gold,
        lambda prediction, expected: prediction in expected["options"],
    )
    return {
        "strict": metric_counts(len(strict_predicted), len(predicted), len(strict_gold)),
        "alias_aware": metric_counts(
            len(alias_predicted), len(predicted), len(alias_gold)
        ),
        "unmatched_predictions": [
            {"from": item[0], "relation": item[1], "to": item[2]}
            for index, item in enumerate(predicted)
            if index not in alias_predicted
        ],
        "unmatched_gold": [
            {
                "from": item["from"],
                "relation": item["relation"],
                "to": item["to"],
            }
            for index, item in enumerate(gold)
            if index not in alias_matches
        ],
    }


async def extract_document(engine: StudyGraphEngine, path: Path) -> dict:
    started = time.perf_counter()
    logger.info("document_processing_started document=%s", path.name)
    read_started = time.perf_counter()
    file_bytes = path.read_bytes()
    logger.info(
        "document_read_complete document=%s bytes=%d duration_seconds=%.2f",
        path.name,
        len(file_bytes),
        time.perf_counter() - read_started,
    )

    parse_started = time.perf_counter()
    pages = await extract_text(file_bytes, path.name)
    text_chars = sum(
        len(page.get("text", ""))
        for page in pages
        if isinstance(page.get("text", ""), str)
    )
    page_sources = {}
    for page in pages:
        source = page.get("source", "native_or_unknown")
        page_sources[source] = page_sources.get(source, 0) + 1
    logger.info(
        "document_parse_complete document=%s pages=%d text_chars=%d page_sources=%s duration_seconds=%.2f",
        path.name,
        len(pages),
        text_chars,
        page_sources,
        time.perf_counter() - parse_started,
    )

    chunk_started = time.perf_counter()
    chunks = engine.get_chunks(pages, source=path.name)
    if not chunks:
        logger.error(
            "document_chunking_empty document=%s pages=%d text_chars=%d",
            path.name,
            len(pages),
            text_chars,
        )
        raise ValueError(f"No text could be extracted from {path}")
    logger.info(
        "document_chunking_complete document=%s chunks=%d duration_seconds=%.2f",
        path.name,
        len(chunks),
        time.perf_counter() - chunk_started,
    )

    predictions = {"concepts": [], "relationships": []}
    batches = make_extraction_batches(chunks)
    logger.info(
        "document_extraction_started document=%s batches=%d chunks=%d",
        path.name,
        len(batches),
        len(chunks),
    )
    extraction_started = time.perf_counter()
    for batch_index, batch in enumerate(batches, start=1):
        batch_started = time.perf_counter()
        logger.info(
            "document_extraction_batch_started document=%s batch=%d/%d chunks=%d chars=%d",
            path.name,
            batch_index,
            len(batches),
            len(batch),
            sum(len(chunk["text"]) for chunk in batch),
        )
        try:
            result = await extract_with_exp_backoff(
                engine,
                batch,
                batch_index=batch_index,
                total_batches=len(batches),
            )
        except Exception:
            logger.exception(
                "document_extraction_batch_failed document=%s batch=%d/%d",
                path.name,
                batch_index,
                len(batches),
            )
            raise
        validated = ExtractionResult(**result)
        predictions["concepts"].extend(
            concept.model_dump() for concept in validated.concepts
        )
        predictions["relationships"].extend(
            relationship.model_dump(by_alias=True)
            for relationship in validated.relationships
        )
        logger.info(
            "document_extraction_batch_complete document=%s batch=%d/%d concepts=%d relationships=%d duration_seconds=%.2f",
            path.name,
            batch_index,
            len(batches),
            len(validated.concepts),
            len(validated.relationships),
            time.perf_counter() - batch_started,
        )
    logger.info(
        "document_extraction_complete document=%s concepts=%d relationships=%d duration_seconds=%.2f",
        path.name,
        len(predictions["concepts"]),
        len(predictions["relationships"]),
        time.perf_counter() - extraction_started,
    )
    logger.info(
        "document_processing_complete document=%s pages=%d chunks=%d batches=%d concepts=%d relationships=%d duration_seconds=%.2f",
        path.name,
        len(pages),
        len(chunks),
        len(batches),
        len(predictions["concepts"]),
        len(predictions["relationships"]),
        time.perf_counter() - started,
    )
    return {
        "predictions": predictions,
        "page_count": len(pages),
        "chunk_count": len(chunks),
        "batch_count": len(batches),
    }


async def evaluate_all() -> dict:
    if not OPENAI_API_KEY:
        logger.error("evaluation_configuration_error reason=openai_api_key_missing")
        raise RuntimeError("OPENAI_API_KEY is not set; extraction evaluation needs it")
    if not DOCS_DIR.is_dir():
        logger.error("evaluation_configuration_error docs_dir_missing=%s", DOCS_DIR)
        raise FileNotFoundError(f"Evaluation document directory not found: {DOCS_DIR}")

    documents = sorted(path for path in DOCS_DIR.iterdir() if path.is_file())
    if not documents:
        logger.error("evaluation_configuration_error no_documents docs_dir=%s", DOCS_DIR)
        raise FileNotFoundError(f"No evaluation documents found in {DOCS_DIR}")

    label_files = list(LABELS_DIR.glob("*.json"))
    logger.info(
        "evaluation_started documents=%d label_files=%d extraction_model=%s docs_dir=%s labels_dir=%s",
        len(documents),
        len(label_files),
        EXTRACTION_MODEL,
        DOCS_DIR,
        LABELS_DIR,
    )
    logger.info(
        "evaluation_storage_initialized type=ephemeral_chroma persistence=disabled"
    )
    chroma = EphemeralClient()
    collection = chroma.create_collection(name="studygraph_concept_eval")
    engine = StudyGraphEngine(
        collections=collection,
        client=create_llm_client(OPENAI_API_KEY),
    )
    document_results = []
    missing_labels = []
    for document_index, document in enumerate(documents, start=1):
        label_path = LABELS_DIR / f"{document.stem}.json"
        logger.info(
            "evaluation_document_started document=%s index=%d/%d label_file=%s label_exists=%s",
            document.name,
            document_index,
            len(documents),
            label_path.name,
            label_path.is_file(),
        )
        try:
            extracted = await extract_document(engine, document)
        except Exception:
            logger.exception(
                "evaluation_document_failed document=%s index=%d/%d",
                document.name,
                document_index,
                len(documents),
            )
            raise
        if not label_path.is_file():
            missing_labels.append(document.name)
            logger.warning(
                "evaluation_document_unscored document=%s reason=gold_labels_missing",
                document.name,
            )
            document_results.append(
                {
                    "document": document.name,
                    "status": "extracted_without_labels",
                    **{
                        key: value
                        for key, value in extracted.items()
                        if key != "predictions"
                    },
                    "predictions": extracted["predictions"],
                }
            )
            logger.info(
                "evaluation_document_complete document=%s index=%d/%d status=unscored",
                document.name,
                document_index,
                len(documents),
            )
            continue

        try:
            labels = load_gold_labels(label_path)
        except Exception:
            logger.exception(
                "evaluation_labels_load_failed document=%s label_file=%s",
                document.name,
                label_path.name,
            )
            raise
        predictions = extracted["predictions"]
        concept_metrics = evaluate_concepts(
            predictions["concepts"], labels["concepts"]
        )
        relationship_metrics = evaluate_relationships(
            predictions["relationships"], gold_relationships(labels)
        )
        logger.info(
            "evaluation_document_scored document=%s label_status=%s predicted_concepts=%d gold_concepts=%d concept_f1_strict=%.3f concept_f1_alias=%.3f predicted_relationships=%d gold_relationships=%d relationship_f1_strict=%.3f relationship_f1_alias=%.3f",
            document.name,
            labels.get("status", "unspecified"),
            len(predictions["concepts"]),
            len(labels["concepts"]),
            concept_metrics["strict"]["f1_score"],
            concept_metrics["alias_aware"]["f1_score"],
            len(predictions["relationships"]),
            len(labels["relationships"]),
            relationship_metrics["strict"]["f1_score"],
            relationship_metrics["alias_aware"]["f1_score"],
        )
        document_results.append(
            {
                "document": document.name,
                "label_file": label_path.name,
                "doc_id": labels.get("doc_id"),
                "label_status": labels.get("status", "unspecified"),
                "page_count": extracted["page_count"],
                "chunk_count": extracted["chunk_count"],
                "batch_count": extracted["batch_count"],
                "concept_metrics": concept_metrics,
                "relationship_metrics": relationship_metrics,
                "predictions": predictions,
            }
        )
        logger.info(
            "evaluation_document_complete document=%s index=%d/%d",
            document.name,
            document_index,
            len(documents),
        )

    matched_results = [
        result for result in document_results if result.get("concept_metrics")
    ]
    matched_label_names = {
        result["label_file"] for result in matched_results
    }
    labels_without_documents = sorted(
        path.name
        for path in LABELS_DIR.glob("*.json")
        if path.name not in matched_label_names
    )
    if missing_labels:
        logger.warning(
            "evaluation_documents_missing_labels count=%d documents=%s",
            len(missing_labels),
            missing_labels,
        )
    if labels_without_documents:
        logger.warning(
            "evaluation_labels_without_documents count=%d labels=%s",
            len(labels_without_documents),
            labels_without_documents,
        )
    summary = {}
    for metric_name in ("concept_metrics", "relationship_metrics"):
        summary[metric_name] = {}
        for scoring_mode in ("strict", "alias_aware"):
            count_keys = ("true_positive", "false_positive", "false_negative")
            totals = {
                key: sum(
                    result[metric_name][scoring_mode][key]
                    for result in matched_results
                )
                for key in count_keys
            }
            micro = metric_counts(
                totals["true_positive"],
                totals["true_positive"] + totals["false_positive"],
                totals["true_positive"] + totals["false_negative"],
            )
            metric_rows = [
                result[metric_name][scoring_mode] for result in matched_results
            ]
            macro = {
                key: (
                    sum(row[key] for row in metric_rows) / len(metric_rows)
                    if metric_rows
                    else 0.0
                )
                for key in ("precision", "recall", "f1_score")
            }
            summary[metric_name][scoring_mode] = {
                "micro": micro,
                "macro": macro,
            }
            logger.info(
                "evaluation_summary metric=%s mode=%s micro_precision=%.3f micro_recall=%.3f micro_f1=%.3f macro_f1=%.3f",
                metric_name,
                scoring_mode,
                micro["precision"],
                micro["recall"],
                micro["f1_score"],
                macro["f1_score"],
            )

    logger.info(
        "evaluation_complete documents_scored=%d documents_unscored=%d labels_unmatched=%d",
        len(matched_results),
        len(missing_labels),
        len(labels_without_documents),
    )
    return {
        "created_at": datetime.now().astimezone().isoformat(),
        "extraction_model": EXTRACTION_MODEL,
        "metric_notes": {
            "strict": "Normalized canonical names and exact directed relation triples.",
            "alias_aware": "A gold concept or relation endpoint may match any annotated alias.",
            "provenance": "Not scored: gold chunk IDs are section/slide labels, while extraction uses generated source chunk IDs.",
            "annotation_quality": "The current annotations are marked draft/unverified; scores are provisional until labels are human-reviewed.",
        },
        "evaluated_documents": len(matched_results),
        "documents_without_labels": missing_labels,
        "labels_without_documents": labels_without_documents,
        "summary": summary,
        "documents": document_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    configure_logging()
    run_started = time.perf_counter()
    logger.info("concept_evaluation_cli_started")
    try:
        report = asyncio.run(evaluate_all())
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / (
            f"concept_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(
            "evaluation_report_written path=%s bytes=%d",
            report_path,
            report_path.stat().st_size,
        )
        logger.info(
            "concept_evaluation_cli_complete report=%s duration_seconds=%.2f",
            report_path,
            time.perf_counter() - run_started,
        )
        print(f"Evaluated {report['evaluated_documents']} labeled document(s).")
        if report["documents_without_labels"]:
            print(
                "Extracted but could not score documents without labels: "
                + ", ".join(report["documents_without_labels"])
            )
        if report["labels_without_documents"]:
            print(
                "Labels without matching source documents: "
                + ", ".join(report["labels_without_documents"])
            )
        print(f"Report: {report_path}")
    except Exception:
        logger.exception(
            "concept_evaluation_cli_failed duration_seconds=%.2f",
            time.perf_counter() - run_started,
        )
        raise


if __name__ == "__main__":
    main()