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


# Single provider switch. Set LLM_PROVIDER=groq in backend/.env to move the
# extraction, fusion, RAG, quiz, and evolution calls to Groq. The provider
# controls the matching API key, model defaults, request parameters, and
# token-budget defaults below.
LLM_PROVIDER = _model_setting("LLM_PROVIDER", "openai").lower()
if LLM_PROVIDER not in {"openai", "groq"}:
    raise RuntimeError("LLM_PROVIDER must be either 'openai' or 'groq'")

LLM_API_KEY_ENV = "OPENAI_API_KEY" if LLM_PROVIDER == "openai" else "GROQ_API_KEY"
_PROVIDER_DEFAULTS = {
    "openai": {"extraction": "gpt-5.6-luna", "rag": "gpt-5.6-luna", "budget": "2000"},
    "groq": {"extraction": "qwen/qwen3.8-27b", "rag": "openai/gpt-oss-20b", "budget": "2000"},
}
_defaults = _PROVIDER_DEFAULTS[LLM_PROVIDER]

# Provider-specific overrides keep model selection in one place.
EXTRACTION_MODEL = _model_setting(
    f"{LLM_PROVIDER.upper()}_EXTRACTION_MODEL", _defaults["extraction"]
)
RAG_MODEL = _model_setting(f"{LLM_PROVIDER.upper()}_RAG_MODEL", _defaults["rag"])
NON_RAG_REASONING_EFFORT = "none"
RAG_REASONING_EFFORT = "medium" if LLM_PROVIDER == "openai" else "none"
EXTRACTION_MAX_COMPLETION_TOKENS = int(
    os.getenv(f"{LLM_PROVIDER.upper()}_EXTRACTION_MAX_COMPLETION_TOKENS", _defaults["budget"])
)
COMPLETION_TOKEN_PARAMETER = "max_completion_tokens" if LLM_PROVIDER == "openai" else "max_tokens"

for directory in (DATA_DIR, OUTPUT_DIR, FUSION_DIR, UPLOADS_DIR):
    directory.mkdir(exist_ok=True)
