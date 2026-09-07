import asyncio

from chromadb import Client as ChromaClient
from groq import Groq

from services.graph_engine import StudyGraphEngine
from services.graph_runtime import get_collection


client: Groq | None = None
chroma: ChromaClient | None = None
engine: StudyGraphEngine | None = None

# Held for the whole of a fusion. merge_graph reads engine.concepts and
# engine.edges, so an upload pipeline waits on this before appending to them.
fusion_lock = asyncio.Lock()

# Upload pipelines currently running. This is the backend's own count, not the
# documents table: that table's status is written by the browser (see
# services/api.js uploadDocument), so a tab closed mid-upload leaves a row on
# "running" for good and would block fusion permanently.
_uploads_in_flight = 0


def upload_started() -> None:
    global _uploads_in_flight
    _uploads_in_flight += 1


def upload_finished() -> None:
    global _uploads_in_flight
    _uploads_in_flight = max(0, _uploads_in_flight - 1)


def uploads_in_flight() -> int:
    return _uploads_in_flight


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