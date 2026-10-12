"""Export raw and fused StudyGraph extraction results without scoring.

Run from ``backend``:
    python -m eval.concept.fused_extraction_export

Each run creates ``eval/concept/reports/fused_export_<timestamp>/`` and writes
one JSON file per source document.  Gold labels are deliberately not read.
"""

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from chromadb import EphemeralClient

from app.config import EXTRACTION_MODEL, OPENAI_API_KEY
from app.logging_config import configure_logging, get_logger
from eval.concept.concept_eval import extract_document
from services.graph_engine import StudyGraphEngine
from services.llm_provider import create_llm_client


EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DOCS_DIR = EVAL_DIR / "source_docs" / "docs"
REPORTS_DIR = EVAL_DIR / "reports"
logger = get_logger(__name__)


def safe_stem(path: Path) -> str:
    """Return a filesystem-safe output filename derived from a document name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._") or "document"


def make_engine(collection_name: str) -> StudyGraphEngine:
    """Create an isolated in-memory engine so documents never fuse together."""
    chroma = EphemeralClient()
    collection = chroma.create_collection(name=collection_name)
    return StudyGraphEngine(
        collections=collection,
        client=create_llm_client(OPENAI_API_KEY),
    )


async def export_document(document: Path, output_dir: Path, index: int) -> dict:
    """Extract one document, run the production fusion step, and write its JSON."""
    engine = make_engine(f"fused_export_{index}")
    extracted = await extract_document(engine, document)
    raw = extracted["predictions"]

    # add_to_graph preserves the normal application representation and provenance.
    engine.add_to_graph(
        {"graph_name": document.stem, **raw},
        doc_name=document.name,
        week_key="export",
    )
    await asyncio.to_thread(engine.fuse_graph)

    canonical_names = sorted(set(engine.canonical_nodes.values()))
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "document": document.name,
        "extraction_model": EXTRACTION_MODEL,
        "fusion_applied": True,
        "metrics_included": False,
        "document_stats": {
            "page_count": extracted["page_count"],
            "chunk_count": extracted["chunk_count"],
            "batch_count": extracted["batch_count"],
        },
        "raw_extraction": raw,
        "fused_graph": {
            "graph_name": engine.graph_name,
            "canonical_nodes": engine.canonical_nodes,
            "nodes": canonical_names,
            "edges": engine.fused_edges,
            "inferred_edges": engine.inferred_edges,
        },
    }
    output_path = output_dir / f"{safe_stem(document)}.json"
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "fused_export_document_complete document=%s raw_concepts=%d fused_nodes=%d raw_edges=%d fused_edges=%d output=%s",
        document.name,
        len(raw["concepts"]),
        len(canonical_names),
        len(raw["relationships"]),
        len(engine.fused_edges),
        output_path,
    )
    return {"document": document.name, "status": "complete", "output": output_path.name}


async def export_all(documents_dir: Path, output_dir: Path) -> list[dict]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set; extraction and fusion need it")
    if not documents_dir.is_dir():
        raise FileNotFoundError(f"Document directory not found: {documents_dir}")

    documents = sorted(path for path in documents_dir.iterdir() if path.is_file())
    if not documents:
        raise FileNotFoundError(f"No documents found in: {documents_dir}")

    results = []
    for index, document in enumerate(documents, start=1):
        try:
            results.append(await export_document(document, output_dir, index))
        except Exception as error:
            logger.exception("fused_export_document_failed document=%s", document.name)
            failure = {
                "created_at": datetime.now().astimezone().isoformat(),
                "document": document.name,
                "fusion_applied": False,
                "metrics_included": False,
                "error": f"{type(error).__name__}: {error}",
            }
            failure_path = output_dir / f"{safe_stem(document)}.failed.json"
            failure_path.write_text(json.dumps(failure, indent=2), encoding="utf-8")
            results.append({"document": document.name, "status": "failed", "output": failure_path.name})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--documents-dir",
        type=Path,
        default=DEFAULT_DOCS_DIR,
        help="Directory containing source documents (default: %(default)s)",
    )
    args = parser.parse_args()
    configure_logging()

    run_dir = REPORTS_DIR / f"fused_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    results = asyncio.run(export_all(args.documents_dir, run_dir))
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now().astimezone().isoformat(),
                "documents_dir": str(args.documents_dir.resolve()),
                "extraction_model": EXTRACTION_MODEL,
                "fusion_applied": True,
                "metrics_included": False,
                "documents": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Fused extraction export: {run_dir}")


if __name__ == "__main__":
    main()
