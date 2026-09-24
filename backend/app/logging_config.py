import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


_CONFIGURED = False


class _ExcludeUvicorn(logging.Filter):
    def filter(self, record):
        return not record.name.startswith("uvicorn")


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_dir = Path(os.getenv("STUDYGRAPH_LOG_DIR", Path(__file__).resolve().parents[1] / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = RotatingFileHandler(
        log_dir / "studygraph.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)

    for logger_name, level in {
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        "groq": logging.WARNING,
        "sentence_transformers": logging.WARNING,
        "huggingface_hub": logging.ERROR,
        "transformers": logging.ERROR,
    }.items():
        logging.getLogger(logger_name).setLevel(level)

    if os.getenv("STUDYGRAPH_LOG_CONSOLE", "1") != "0":
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(_ExcludeUvicorn())
        root_logger.addHandler(console_handler)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "logging_configured level=%s file=%s",
        log_level,
        file_handler.baseFilename,
    )
