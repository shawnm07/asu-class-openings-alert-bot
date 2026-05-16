import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(name: str) -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return logging.getLogger(name)

    root.setLevel(logging.INFO)
    fmt = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        _LOG_DIR / f"{name}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    return logging.getLogger(name)
