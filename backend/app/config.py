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


# Single source of truth for every Groq chat-completion feature.
EXTRACTION_MODEL = _model_setting("GROQ_EXTRACTION_MODEL", "qwen/qwen3.8-27b")
RAG_MODEL = _model_setting("GROQ_RAG_MODEL", "openai/gpt-oss-20b")

for directory in (DATA_DIR, OUTPUT_DIR, FUSION_DIR, UPLOADS_DIR):
    directory.mkdir(exist_ok=True)
