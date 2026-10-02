from __future__ import annotations

import math

from sentence_transformers import SentenceTransformer


EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384
_model: SentenceTransformer | None = None


def get_embedding_model() -> SentenceTransformer:
	global _model
	if _model is None:
		_model = SentenceTransformer("all-MiniLM-L6-v2")
	return _model


def encode_texts(texts: list[str], model: SentenceTransformer | None = None) -> list[list[float]]:
	if not texts:
		return []
	active_model = model or get_embedding_model()
	vectors = active_model.encode(texts, show_progress_bar=False)
	encoded = [[float(value) for value in vector] for vector in vectors]
	if any(len(vector) != EMBEDDING_DIMENSIONS for vector in encoded):
		raise ValueError(f"Embedding model must return {EMBEDDING_DIMENSIONS}-dimensional vectors.")
	if any(not math.isfinite(value) for vector in encoded for value in vector):
		raise ValueError("Embedding model returned a non-finite value.")
	return encoded