# Concept and Relationship Extraction Evaluation

Run from the `backend` directory:

```powershell
python -m eval.concept.concept_eval
```

The evaluator processes each file in `eval/concept/source_docs/docs`. It uses
the production document parser, `StudyGraphEngine.get_chunks`,
`make_extraction_batches`, and `extract_with_exp_backoff`. Chunk embeddings
are written only to a fresh in-memory Chroma collection; no user graph or
persisted vector collection is changed. The evaluator compares raw extraction
output with matching JSON labels and does not run graph fusion.

Each source file is matched to `eval/concept/gold_labels/<source-stem>.json`.
For example, `synthetic_05_bond_prices.md.pdf` uses
`synthetic_05_bond_prices.md.json`. Files without a matching label are still
extracted, included in the report, and excluded from aggregate metrics.

The timestamped JSON report is written to `eval/concept/reports/`. It includes
per-document and aggregate micro/macro precision, recall, F1, Jaccard accuracy,
and exact-set match for concepts and directed relationship triples. Strict
scoring uses normalized canonical names. Alias-aware scoring also accepts
annotated concept aliases. The current gold labels are marked as drafts, so
scores should be treated as provisional until annotations have been reviewed.

Progress is logged to the console and the standard StudyGraph log at
`backend/logs/studygraph.log`. Logs show document reading and parsing,
page-source counts, chunk and batch progress, extracted item counts, per-file
metrics, aggregate metrics, and report location. Set `LOG_LEVEL=DEBUG` for
additional detail from the rest of the backend.

The run requires configured extraction and embedding API credentials. It can
incur API costs for PDF OCR, embeddings, and model extraction.
