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


def get_embedding_client() -> OpenAI:
    global _client

    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        _client = OpenAI(api_key=OPENAI_API_KEY)

    return _client


def encode_texts(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []

    if any(not isinstance(text, str) for text in texts):
        raise TypeError("All embedding inputs must be strings.")

    response = get_embedding_client().embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=list(texts),
        dimensions=OPENAI_EMBEDDING_DIMENSIONS,
    )

    # The API normally returns items in input order, but sorting by index
    # makes that  explicit.
    data = sorted(response.data, key=lambda item: item.index)
    encoded = [[float(value) for value in item.embedding] for item in data]

	# 384 dims is currently set, eventuall increase this
    if len(encoded) != len(texts):
        raise ValueError("OpenAI returned an unexpected number of embeddings.")

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