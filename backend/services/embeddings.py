from __future__ import annotations

import math
from typing import Sequence
from openai import OpenAI

from app.config import (
	OPENAI_API_KEY,
	OPENAI_EMBEDDING_DIMENSIONS,
	OPENAI_EMBEDDING_MODEL,
)

_client: OpenAI | None = None
_embedding_cache: dict[str, list[float]] = {}
_EMBEDDING_CACHE_MAX = 20000


def get_embedding_client() -> OpenAI:
    global _client

    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        _client = OpenAI(api_key=OPENAI_API_KEY)

    return _client


def encode_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed unique uncached texts, then restore request order and validate the vectors."""
    if not texts:
        return []

    if any(not isinstance(text, str) for text in texts):
        raise TypeError("All embedding inputs must be strings.")

    requested = list(texts)
    missing = []
    missing_seen = set()
    for text in requested:
        if text not in _embedding_cache and text not in missing_seen:
            missing.append(text)
            missing_seen.add(text)

    if missing:
        response = get_embedding_client().embeddings.create(
            model=OPENAI_EMBEDDING_MODEL,
            input=missing,
            dimensions=OPENAI_EMBEDDING_DIMENSIONS,
        )

        # The API normally returns items in input order, but sorting by index
        # makes that explicit.
        data = sorted(response.data, key=lambda item: item.index)
        encoded_missing = [[float(value) for value in item.embedding] for item in data]
        if len(encoded_missing) != len(missing):
            raise ValueError("OpenAI returned an unexpected number of embeddings.")
        for text, vector in zip(missing, encoded_missing):
            _embedding_cache[text] = vector
        if len(_embedding_cache) > _EMBEDDING_CACHE_MAX:
            # Keep the cache bounded; correctness does not depend on cache hits.
            for old_text in list(_embedding_cache)[:len(_embedding_cache) - _EMBEDDING_CACHE_MAX]:
                del _embedding_cache[old_text]

    encoded = [_embedding_cache[text] for text in requested]

	# 384 dims is currently set, eventuall increase this
    if any(len(vector) != OPENAI_EMBEDDING_DIMENSIONS for vector in encoded):
        raise ValueError(
            f"Embedding model must return "
            f"{OPENAI_EMBEDDING_DIMENSIONS}-dimensional vectors."
        )

    if any(
        not math.isfinite(value)
        for vector in encoded
        for value in vector
    ):
        raise ValueError("OpenAI returned a non-finite embedding value.")

    return encoded
