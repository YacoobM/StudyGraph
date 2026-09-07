from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"
OUTPUT_DIR = DATA_DIR / "outputs"
FUSION_DIR = OUTPUT_DIR / "fusion"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "studygraph.db"

for directory in (DATA_DIR, OUTPUT_DIR, FUSION_DIR, UPLOADS_DIR):
    directory.mkdir(exist_ok=True)