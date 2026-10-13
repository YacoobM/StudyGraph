# RAGAS Evaluation

This directory contains scripts and utilities for evaluating the StudyGraph RAG pipeline using the **RAGAS** library.

## Prerequisites

- Python 3.10+.
- Dependencies installed:

```powershell
cd backend
pip install -r requirements.txt
```

Put your `OPENAI_API_KEY` in `backend/.env` once; the backend loads it automatically. You do not need to set evaluation environment variables before each run. Model overrides remain optional.

Put supported documents (`.pdf`, `.docx`, `.pptx`, `.txt`, or `.md`) in separate subdirectories of `backend/eval/docs/`. Each immediate subdirectory with supported files is one independent evaluation set.

## Running the evaluation

Run these commands from the `backend/` directory. Manual mode is the default and reads questions from JSONL files in `eval/data/`; that same question set is evaluated separately against each document directory:

```powershell
python -m eval.ragas_evaluation --manual --num-questions 20 --num-directories 2
```

`--num-directories` (or `-d`) limits the run to the first N non-empty subdirectories alphabetically; omit it to run all document sets. `--num-questions` (or `-n`) controls the question count. In manual mode it evaluates up to that many questions; omit it to use all available questions.

For synthetic questions generated separately from each document directory, use:

```powershell
python -m eval.ragas_evaluation --synthetic --num-questions 20 --num-directories 2
```

Synthetic mode generates 10 questions per document set by default if the count is omitted. Each directory gets a fresh graph and vector collection so documents from another set cannot affect its retrieval results.

For each set, the evaluator extracts text, indexes chunks with the app's OpenAI embedding configuration, extracts concepts and relationships with the app's graph-extraction model, and fuses the graph before running queries through `StudyGraphEngine.rag_query()`. This includes graph extraction API calls in addition to answer generation and Ragas scoring.

Each set writes two JSON files under `eval/reports/`:

- `ragas_results_<directory>_<timestamp>.json` contains the per-question RAGAS scores and evaluation metadata. It is saved immediately after RAGAS finishes.
- `ragas_aggregate_<directory>_<timestamp>.json` contains the average for each metric and `overall_score`, the mean of those metric averages.

If aggregation fails after the per-question report is saved, rerun only that step using its path:

```powershell
python -m eval.ragas_evaluation --aggregate-report eval/reports/ragas_results_test1_20261013_080000.json
```

This aggregation-only command does not need an API key or rerun graph extraction, RAG queries, or RAGAS scoring.

Ragas scoring runs with at most four concurrent workers and retries failed operations up to ten times with exponential backoff, to reduce failures from transient API connection drops.

## Interpreting the report

The aggregate JSON contains `overall_metrics` and the source report path. The per-question results JSON contains a `results` list of dictionaries with the question, answer, ground truth, retrieved contexts, and individual metric scores (`answer_relevancy`, `faithfulness`, `answer_similarity`, `context_recall`).

Higher metric averages indicate better performance. Scores missing for an individual question are excluded from that metric's average.

## Manual question data

Place one or more `.jsonl` files in `backend/eval/data/`. Each non-empty line should be a JSON object with a `question` and `ground_truth`, for example:

```json
{"question":"What is the main idea?","ground_truth":"The reference answer."}
```

## Extending the evaluation

- **Additional metrics**: Import any other RAGAS metrics and include them in the `metrics` list.

---