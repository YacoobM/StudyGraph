from __future__ import annotations

from chromadb import Client as ChromaClient
from groq import Groq

from services.graph_engine import StudyGraphEngine
from services.graph_runtime import get_collection


client: Groq | None = None
chroma: ChromaClient | None = None
engine: StudyGraphEngine | None = None


def configure_runtime(groq_client: Groq, chroma_client: ChromaClient) -> None:
    global client, chroma
    client = groq_client
    chroma = chroma_client


def get_engine() -> StudyGraphEngine:
    global engine
    if engine is None:
        if client is None or chroma is None:
            raise RuntimeError("Application runtime has not been configured")
        engine = StudyGraphEngine(get_collection(chroma, 1), client)
    return engine


def set_engine(value: StudyGraphEngine) -> None:
    global engine
    engine = value