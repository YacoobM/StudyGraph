import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parents[1]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

from app import state as runtime_state
from app.config import LLM_API_KEY_ENV
from services.llm_provider import create_llm_client
from app.logging_config import get_logger

# API routes registration
from api.routes.graphs import router as graphs_router
from api.routes.documents import router as documents_router
from api.routes.query import router as query_router
from api.routes.quiz import router as quiz_router
from api.routes.notes import router as notes_router


logger = get_logger(__name__)
load_dotenv()

if not os.getenv(LLM_API_KEY_ENV):
    raise RuntimeError(f"{LLM_API_KEY_ENV} is not configured!")

app = FastAPI(
    title="StudyGraph API",
    description="FastAPI backend for StudyGraph",
    version="0.1.0"
)


CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    os.getenv("FRONTEND_URL", "http://localhost:3000"), #depending on where we deploy
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(graphs_router)
app.include_router(documents_router)
app.include_router(query_router)
app.include_router(quiz_router)
app.include_router(notes_router)

client = create_llm_client(os.environ[LLM_API_KEY_ENV])

runtime_state.configure_runtime(client)


@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)

