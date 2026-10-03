from __future__ import annotations

import asyncio
from typing import Any
from services.graph_engine import StudyGraphEngine

client: Any | None = None
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


def configure_runtime(llm_client: Any) -> None:
    global client
    client = llm_client

