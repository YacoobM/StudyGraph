"""Run Ragas against StudyGraph using documents in docs/ and JSONL questions in data/."""

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
from chromadb import Client as ChromaClient
from datasets import Dataset
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from ragas import evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (
    answer_relevancy,
    answer_similarity,
    context_recall,
    faithfulness,
)
from ragas.run_config import RunConfig
from ragas.testset import TestsetGenerator
from ragas.testset.transforms import HeadlineSplitter, default_transforms
from tqdm import tqdm

from app.config import OPENAI_API_KEY, RAG_MODEL
from app.state import configure_runtime
from services.document_service import (
    extract_text,
    extract_with_exp_backoff,
    make_extraction_batches,
)
from services.graph_engine import StudyGraphEngine
from services.llm_provider import create_llm_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ragas_evaluation")

EVAL_DIR = Path(__file__).parent
DOCS_DIR = EVAL_DIR / "docs"
DATA_DIR = EVAL_DIR / "data"
REPORTS_DIR = EVAL_DIR / "reports"

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md"}


class LunaCompatibleChatOpenAI(ChatOpenAI):
    """ChatOpenAI client that ignores unsupported Ragas temperature overrides."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload.pop("temperature", None)
        return payload


def build_ragas_models(api_key: str, model: str):
    """Build LangChain LLM + embeddings wrapped for Ragas.

    Ragas creates/tears down asyncio loops internally, so HTTP keep-alive is
    disabled to stop pooled connections from one loop being reused by another.
    """
    limits = httpx.Limits(max_keepalive_connections=0)
    llm = LangchainLLMWrapper(LunaCompatibleChatOpenAI(
        model=model,
        api_key=api_key,
        model_kwargs={"response_format": {"type": "json_object"}},
        http_client=httpx.Client(limits=limits),
        http_async_client=httpx.AsyncClient(limits=limits),
    ))
    embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
        model=os.getenv("RAGAS_EMBEDDING_MODEL", "text-embedding-3-small"),
        api_key=api_key,
        http_client=httpx.Client(limits=limits),
        http_async_client=httpx.AsyncClient(limits=limits),
    ))
    return llm, embeddings


async def load_document_pages(path: Path) -> list[dict]:
    """Load supported files into the engine's page-like format."""
    return await extract_text(path.read_bytes(), path.name)


def load_questions(limit: int | None = None) -> list[dict]:
    files = sorted(DATA_DIR.glob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"No JSONL evaluation set found in {DATA_DIR}")
    rows = []
    for path in files:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                question = item.get(
                    "question", item.get("query", item.get("user_input", ""))
                ).strip()
                if not question:
                    raise ValueError(f"Missing question in {path}:{line_number}")
                rows.append({
                    "question": question,
                    "ground_truth": item.get(
                        "ground_truth",
                        item.get("ground_truth_answer", item.get("reference", "")),
                    ),
                })
    return rows[:limit] if limit is not None else rows


def generate_questions(
    docs: list[Path],
    document_pages: dict[Path, list[dict]],
    api_key: str,
    size: int,
    dataset_name: str,
    output_dir: Path,
) -> list[dict]:
    """Generate a synthetic Ragas test set from the evaluation documents."""
    logger.info("Preparing document pages for synthetic question generation")
    
    documents = []
    for doc in docs:
        pages = document_pages[doc]
        full_text = " ".join(p["text"] for p in pages)
        
        if len(full_text.split()) > 100:
            documents.append(
                Document(
                    page_content=full_text,
                    metadata={"source": doc.name}
                )
            )
            logger.info(f"  {doc.name}: {len(full_text.split())} tokens")
    
    if not documents:
        raise ValueError("No documents with >100 tokens found!")
    
    logger.info(f"Created {len(documents)} documents for RAGAS")
    
    logger.info("Generating %d synthetic questions", size)

    llm, embeddings = build_ragas_models(
        api_key, os.getenv("RAGAS_GENERATION_MODEL", RAG_MODEL)
    )
    generator = TestsetGenerator(llm=llm, embedding_model=embeddings)
    transforms = default_transforms(
        documents,
        llm=generator.llm,
        embedding_model=generator.embedding_model,
    )
    for transform in transforms:
        if isinstance(transform, HeadlineSplitter):
            transform.filter_nodes = (
                lambda node: node.get_property("headlines") is not None
            )
    testset = generator.generate_with_langchain_docs(
        documents,
        testset_size=size,
        transforms=transforms,
    )
    logger.info("Synthetic question generation complete")

    generated = testset.to_pandas()
    generated.to_json(
        output_dir / f"generated_testset_{dataset_name}.jsonl",
        orient="records",
        lines=True,
    )
    return [
        {
            "question": row.get("user_input", row.get("question", "")),
            "ground_truth": row.get("reference", row.get("ground_truth", "")),
        }
        for row in generated.to_dict(orient="records")
    ]


def positive_int(value: str) -> int:
    """Parse a positive integer for the requested evaluation question count."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate StudyGraph's RAG pipeline with Ragas."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--synthetic",
        dest="mode",
        action="store_const",
        const="synthetic",
        help="Generate questions from each selected document set.",
    )
    mode.add_argument(
        "--manual",
        dest="mode",
        action="store_const",
        const="manual",
        help="Use questions from JSONL files in backend/eval/data (default).",
    )
    parser.set_defaults(mode="manual")
    parser.add_argument(
        "-n",
        "--num-questions",
        type=positive_int,
        help="Number of questions to evaluate (manual mode uses up to this many).",
    )
    parser.add_argument(
        "-d",
        "--num-directories",
        type=positive_int,
        help="Number of document subdirectories to evaluate, in alphabetical order (default: all).",
    )
    parser.add_argument(
        "--aggregate-report",
        type=Path,
        metavar="PATH",
        help="Aggregate scores from an existing per-question results JSON without rerunning evaluation.",
    )
    return parser.parse_args(argv)


def find_document_sets(limit: int | None = None) -> list[tuple[str, list[Path]]]:
    """Return supported documents grouped by immediate child directory."""
    document_sets = []
    for directory in sorted(path for path in DOCS_DIR.iterdir() if path.is_dir()):
        documents = sorted(
            path for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if documents:
            document_sets.append((directory.name, documents))
        else:
            logger.warning("Skipping %s: no supported documents found", directory)

    if not document_sets:
        root_documents = sorted(
            path for path in DOCS_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if root_documents:
            document_sets.append(("root", root_documents))
    if not document_sets:
        raise FileNotFoundError(
            f"Add supported documents to subdirectories of {DOCS_DIR}"
        )
    return document_sets[:limit] if limit is not None else document_sets


def evaluate_document_set(
    dataset_name: str,
    documents: list[Path],
    mode: str,
    num_questions: int | None,
    api_key: str,
    llm_client,
    general_dir: Path,
    aggregate_dir: Path,
    timestamp: str,
) -> Path:
    """Run indexing, RAG queries, and Ragas scoring for one document set."""
    logger.info("Starting document set '%s' (%d document(s))", dataset_name, len(documents))
    chroma_client = ChromaClient()
    collection = chroma_client.get_or_create_collection(
        name=f"ragas_{dataset_name[:40]}"
    )
    engine = StudyGraphEngine(collections=collection, client=llm_client)
    document_pages = asyncio.run(index_documents_and_build_graph(
        documents,
        engine,
        dataset_name,
    ))

    if mode == "synthetic":
        logger.info("[%s] Generating synthetic questions", dataset_name)
        questions = generate_questions(
            documents,
            document_pages,
            api_key,
            num_questions or 10,
            dataset_name,
            general_dir,
        )
    else:
        logger.info("[%s] Loading manual questions", dataset_name)
        questions = load_questions(num_questions)
    if not questions:
        raise ValueError(f"No evaluation questions found for '{dataset_name}'")
    logger.info("[%s] Loaded %d question(s)", dataset_name, len(questions))

    logger.info("[%s] Running RAG queries", dataset_name)
    eval_data = []
    for number, item in enumerate(tqdm(questions, desc="RAG queries"), 1):
        logger.info(
            "[%s] RAG query %d/%d: %s",
            dataset_name,
            number,
            len(questions),
            item["question"][:100],
        )
        result = engine.rag_query(item["question"])
        eval_data.append({
            "question": item["question"],
            "answer": result.get("answer", ""),
            "contexts": [c.get("text", "") for c in result.get("chunks", [])],
            "ground_truth": item["ground_truth"],
        })

    metrics = [answer_relevancy]
    if all(row["ground_truth"] for row in eval_data):
        metrics += [faithfulness, answer_similarity, context_recall]

    logger.info(
        "[%s] Evaluating with Ragas (%d metric(s))", dataset_name, len(metrics)
    )
    llm, embeddings = build_ragas_models(
        api_key, os.getenv("RAGAS_EVALUATION_MODEL", RAG_MODEL)
    )
    results = evaluate(
        Dataset.from_list(eval_data),
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        run_config=RunConfig(max_workers=4, max_retries=10, max_wait=60),
        raise_exceptions=True,
    )
    logger.info("[%s] Ragas metric evaluation complete", dataset_name)

    report_path = general_dir / f"ragas_results_{dataset_name}_{timestamp}.json"
    results_frame = results.to_pandas()
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "dataset": dataset_name,
                "evaluated_at": timestamp,
                "metrics": [metric.name for metric in metrics],
                "results": results_frame.to_dict(orient="records"),
            },
            f, indent=2, ensure_ascii=False, default=float,
        )
    logger.info("[%s] Per-question results saved to %s", dataset_name, report_path)
    return aggregate_results_report(report_path, aggregate_dir)


def aggregate_results_report(
    results_path: Path, aggregate_dir: Path | None = None
) -> Path:
    """Create a separate aggregate report from a saved per-question report."""
    with results_path.open(encoding="utf-8") as f:
        report = json.load(f)
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise ValueError(f"Invalid per-question RAGAS report: {results_path}")
    if not isinstance(report.get("metrics"), list):
        raise ValueError(f"RAGAS report is missing metric names: {results_path}")

    results_frame = pd.DataFrame(report["results"])
    overall_metrics = {}
    for column in report["metrics"]:
        if column not in results_frame.columns:
            continue
        scores = pd.to_numeric(results_frame[column], errors="coerce").dropna()
        if not scores.empty:
            overall_metrics[column] = float(scores.mean())
    if overall_metrics:
        overall_metrics["overall_score"] = (
            sum(overall_metrics.values()) / len(overall_metrics)
        )

    dataset_name = report.get("dataset", results_path.stem)
    evaluated_at = report.get("evaluated_at", datetime.now().strftime("%Y%m%d_%H%M%S"))
    aggregate_dir = aggregate_dir or (
        results_path.parent.parent / "aggregate"
        if results_path.parent.name == "general"
        else REPORTS_DIR / "aggregate"
    )
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    aggregate_path = aggregate_dir / (
        f"ragas_aggregate_{dataset_name}_{evaluated_at}.json"
    )
    with aggregate_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "dataset": dataset_name,
                "evaluated_at": evaluated_at,
                "source_report": str(results_path),
                "question_count": len(report["results"]),
                "overall_metrics": overall_metrics,
            },
            f, indent=2, ensure_ascii=False, default=float,
        )
    logger.info(
        "[%s] Average Ragas metrics across %d question(s): %s",
        dataset_name,
        len(report["results"]),
        ", ".join(
            f"{name}={score:.4f}" for name, score in overall_metrics.items()
        ) or "no metric scores available",
    )
    logger.info("[%s] Aggregate report saved to %s", dataset_name, aggregate_path)
    return aggregate_path


async def index_documents_and_build_graph(
    documents: list[Path],
    engine: StudyGraphEngine,
    dataset_name: str,
) -> dict[Path, list[dict]]:
    """Index documents and run the production graph extraction/fusion steps."""
    document_pages = {}
    logger.info("[%s] Extracting and indexing %d document(s)", dataset_name, len(documents))
    for doc in tqdm(documents, desc=f"Indexing {dataset_name}"):
        pages = await load_document_pages(doc)
        document_pages[doc] = pages
        logger.info(
            "  %s: extracted %d pages, %d chars",
            doc.name,
            len(pages),
            sum(len(page.get("text", "")) for page in pages),
        )
        if not pages:
            logger.warning("  %s: NO PAGES EXTRACTED", doc.name)
            continue

        chunks = engine.get_chunks(pages, source=doc.name)
        logger.info("  %s: indexed %d chunks", doc.name, len(chunks))
        batches = make_extraction_batches(chunks)
        for batch_number, batch in enumerate(batches, 1):
            known_values = await asyncio.gather(*(
                asyncio.to_thread(engine.relevant_known_concepts, chunk["text"])
                for chunk in batch
            ))
            known_concepts = {
                chunk["chunk_id"]: known
                for chunk, known in zip(batch, known_values)
            }
            extracted = await extract_with_exp_backoff(
                engine,
                batch,
                known_concepts_by_chunk=known_concepts,
                batch_index=batch_number,
                total_batches=len(batches),
            )
            batch_pages = sorted({
                page
                for chunk in batch
                for page in (chunk.get("pages") or [])
            })
            await asyncio.to_thread(
                engine.add_to_graph,
                extracted,
                doc.name,
                f"Set {dataset_name}",
                pages=batch_pages,
            )
            engine.known_concepts.update(
                concept["name"]
                for concept in engine.concepts
                if concept.get("source") == doc.name
            )
            await asyncio.to_thread(engine.rebuild_known_concept_embeddings)

    logger.info("[%s] Deduplicating and fusing extracted graph", dataset_name)
    await asyncio.to_thread(engine.deduplicate_extracted_edges)
    await asyncio.to_thread(engine.fuse_graph)
    if engine.concepts and not (engine.edges or engine.fused_edges):
        logger.warning(
            "[%s] Graph extraction produced concepts but no relationships",
            dataset_name,
        )
    logger.info(
        "[%s] Graph ready: %d concepts, %d relationships",
        dataset_name,
        len(engine.concepts),
        len(engine.fused_edges or engine.edges),
    )
    return document_pages


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.aggregate_report is not None:
        aggregate_results_report(args.aggregate_report)
        return

    logger.info("Starting Ragas evaluation")
    api_key = OPENAI_API_KEY
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Add it to backend/.env or your environment."
        )

    document_sets = find_document_sets(args.num_directories)
    if not document_sets:
        raise FileNotFoundError(f"No document sets found in {DOCS_DIR}")
    logger.info("Evaluating %d document set(s)", len(document_sets))

    logger.info("Initializing StudyGraph runtime")
    llm_client = create_llm_client(api_key)
    configure_runtime(llm_client)
    logger.info("StudyGraph runtime ready")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = REPORTS_DIR / f"run_{timestamp}"
    general_dir, aggregate_dir = run_dir / "general", run_dir / "aggregate"
    general_dir.mkdir(parents=True)
    aggregate_dir.mkdir()
    for dataset_name, documents in document_sets:
        evaluate_document_set(
            dataset_name,
            documents,
            args.mode,
            args.num_questions,
            api_key,
            llm_client,
            general_dir,
            aggregate_dir,
            timestamp,
        )
    logger.info("Ragas evaluation complete for %d document set(s)", len(document_sets))


if __name__ == "__main__":
    main()