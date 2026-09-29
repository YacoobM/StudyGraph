"""Run Ragas against StudyGraph using documents in docs/ and JSONL questions in data/."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
from chromadb import Client as ChromaClient
from datasets import Dataset
from dotenv import load_dotenv
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
from ragas.testset import TestsetGenerator
from tqdm import tqdm

from app.config import LLM_API_KEY_ENV, RAG_MODEL
from app.state import configure_runtime, get_engine
from services.document_service import extract_text
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


def load_document_pages(path: Path) -> list[dict]:
    """Load supported files into the engine's page-like format."""
    return extract_text(path.read_bytes(), path.name)


def load_questions() -> list[dict]:
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
    return rows


def generate_questions(docs: list[Path], api_key: str) -> list[dict]:
    """Generate a synthetic Ragas test set from the evaluation documents."""
    if os.getenv("LLM_PROVIDER", "openai").lower() != "openai":
        raise RuntimeError("Synthetic generation currently requires LLM_PROVIDER=openai")

    logger.info("Preparing document pages for synthetic question generation")
    
    documents = []
    for doc in docs:
        pages = load_document_pages(doc)
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
    
    size = int(os.getenv("RAGAS_TESTSET_SIZE", "10"))
    logger.info("Generating %d synthetic questions", size)

    llm, embeddings = build_ragas_models(
        api_key, os.getenv("RAGAS_GENERATION_MODEL", RAG_MODEL)
    )
    generator = TestsetGenerator(llm=llm, embedding_model=embeddings)
    testset = generator.generate_with_langchain_docs(documents, testset_size=size)
    logger.info("Synthetic question generation complete")

    generated = testset.to_pandas()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    generated.to_json(REPORTS_DIR / "generated_testset.jsonl", orient="records", lines=True)
    return [
        {
            "question": row.get("user_input", row.get("question", "")),
            "ground_truth": row.get("reference", row.get("ground_truth", "")),
        }
        for row in generated.to_dict(orient="records")
    ]


def main() -> None:
    logger.info("Starting Ragas evaluation")
    load_dotenv()
    api_key = os.getenv(LLM_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{LLM_API_KEY_ENV} not set in environment")

    logger.info("Initializing StudyGraph runtime and embedding model")
    configure_runtime(create_llm_client(api_key), ChromaClient())
    engine = get_engine()
    logger.info("StudyGraph runtime ready")

    documents = sorted(
        path for path in DOCS_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not documents:
        raise FileNotFoundError(f"Add a PDF, DOCX, PPTX, TXT, or MD file to {DOCS_DIR}")

    # Delete collections once before running
    all_ids = engine.collections.get()["ids"]
    if all_ids:
        engine.collections.delete(ids=all_ids)
        logger.info(f"Deleted {len(all_ids)} chunks")
    
    logger.info("[1/4] Indexing %d document(s)", len(documents))
    for doc in tqdm(documents, desc="Indexing documents"):
        pages = load_document_pages(doc)
        logger.info(f"  {doc.name}: extracted {len(pages)} pages, {sum(len(p.get('text','')) for p in pages):,} chars")
        
        if pages:
            chunks = engine.get_chunks(pages, source=doc.name)
            logger.info(f"  {doc.name}: indexed {len(chunks) if chunks else 0} chunks")
        else:
            logger.warning(f"  {doc.name}: NO PAGES EXTRACTED")
    logger.info("[1/4] Document indexing complete")

    mode = os.getenv("RAGAS_MODE", "manual").lower()
    if mode == "synthetic":
        logger.info("[2/4] Generating synthetic questions")
        questions = generate_questions(documents, api_key)
    elif mode == "manual":
        logger.info("[2/4] Loading manual questions")
        questions = load_questions()
    else:
        raise ValueError("RAGAS_MODE must be either 'manual' or 'synthetic'")
    logger.info("[2/4] Loaded %d question(s)", len(questions))

    logger.info("[3/4] Running RAG queries")
    eval_data = []
    for number, item in enumerate(tqdm(questions, desc="RAG queries"), 1):
        logger.info("RAG query %d/%d: %s", number, len(questions), item["question"][:100])
        result = engine.rag_query(item["question"])
        eval_data.append({
            "question": item["question"],
            "answer": result.get("answer", ""),
            "contexts": [c.get("text", "") for c in result.get("chunks", [])],
            "ground_truth": item["ground_truth"],
        })
    logger.info("[3/4] RAG queries complete")

    metrics = [answer_relevancy]
    if all(row["ground_truth"] for row in eval_data):
        metrics += [faithfulness, answer_similarity, context_recall]

    logger.info("[4/4] Evaluating with Ragas (%d metric(s))", len(metrics))
    llm, embeddings = build_ragas_models(
        api_key, os.getenv("RAGAS_EVALUATION_MODEL", RAG_MODEL)
    )
    results = evaluate(
        Dataset.from_list(eval_data),
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        raise_exceptions=True,
    )
    logger.info("Ragas metric evaluation complete")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"ragas_report_{timestamp}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            results.to_pandas().to_dict(orient="records"),
            f, indent=2, ensure_ascii=False, default=float,
        )
    logger.info("Ragas evaluation completed. Report saved to %s", report_path)


if __name__ == "__main__":
    main()