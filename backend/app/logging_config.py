import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


_CONFIGURED = False


def configure_logging() -> None:
    """Configure root logger with file and optional console output."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_dir = Path(
        os.getenv(
            "STUDYGRAPH_LOG_DIR",
            Path(__file__).resolve().parents[1] / "logs"
        )
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    # Configurable rotation settings (Tier 5 issue from the doc)
    log_max_bytes = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
    log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    
    file_handler = RotatingFileHandler(
        log_dir / "studygraph.log",
        maxBytes=log_max_bytes,
        backupCount=log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)

    # Suppress verbose third-party loggers
    for logger_name, level in {
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        "openai": logging.WARNING,
        "sentence_transformers": logging.WARNING,
        "huggingface_hub": logging.ERROR,
        "transformers": logging.ERROR,
        "uvicorn.access": logging.WARNING,  # Cleaner than custom filter
        "uvicorn.error": logging.WARNING,
    }.items():
        logging.getLogger(logger_name).setLevel(level)

    # Optional console output
    if os.getenv("STUDYGRAPH_LOG_CONSOLE", "1") != "0":
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "logging_configured level=%s file=%s max_bytes=%d backup_count=%d",
        log_level,
        file_handler.baseFilename,
        log_max_bytes,
        log_backup_count,
    )


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger instance for the given module name."""
    return logging.getLogger(name)