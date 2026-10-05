import os
from pathlib import Path

from dotenv import load_dotenv


# Read backend/.env before resolving model overrides. Process environment
# variables still take precedence for deployments.
load_dotenv()


BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"
OUTPUT_DIR = DATA_DIR / "outputs"
FUSION_DIR = OUTPUT_DIR / "fusion"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "studygraph.db"
SUPABASE_URL = os.getenv("SUPABASE_URL", os.getenv("VITE_SUPABASE_URL", "")).rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv(
    "SUPABASE_PUBLISHABLE_KEY", os.getenv("VITE_SUPABASE_PUBLISHABLE_KEY", "")
)


def _model_setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


EXTRACTION_MODEL = _model_setting("OPENAI_EXTRACTION_MODEL", "gpt-5.6-luna")
RAG_MODEL = _model_setting("OPENAI_RAG_MODEL", "gpt-5.6-luna")
NON_RAG_REASONING_EFFORT = "none"
RAG_REASONING_EFFORT = "medium"
EXTRACTION_MAX_COMPLETION_TOKENS = int(
    os.getenv("OPENAI_EXTRACTION_MAX_COMPLETION_TOKENS", "2000")
)
COMPLETION_TOKEN_PARAMETER = "max_completion_tokens"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
RAG_INPUT_TPM = int(os.getenv("RAG_INPUT_TPM", "100000"))
RAG_OUTPUT_TPM = int(os.getenv("RAG_OUTPUT_TPM", "50000"))
GRAPH_INPUT_TPM = int(os.getenv("GRAPH_INPUT_TPM", "200000"))
GRAPH_OUTPUT_TPM = int(os.getenv("GRAPH_OUTPUT_TPM", "100000"))

OPENAI_EMBEDDING_MODEL = _model_setting(
    "OPENAI_EMBEDDING_MODEL",
    "text-embedding-3-small",
)

OPENAI_EMBEDDING_DIMENSIONS = int(
    os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "384")
)

if OPENAI_EMBEDDING_DIMENSIONS <= 0:
    raise RuntimeError("OPENAI_EMBEDDING_DIMENSIONS must be positive.")

for directory in (DATA_DIR, OUTPUT_DIR, FUSION_DIR, UPLOADS_DIR):
    directory.mkdir(exist_ok=True)
