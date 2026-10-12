"""Application logging configuration for development and production."""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


DEV = "dev"
PROD = "prod"
LOGGER_NAME = "hodowo"


class JsonFormatter(logging.Formatter):
    """Format production logs as single-line JSON records."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging() -> str:
    """Configure application logging and return the active environment."""
    environment = os.getenv("HODOWO_ENV", DEV).strip().lower()
    if environment not in {DEV, PROD}:
        raise RuntimeError("HODOWO_ENV must be either 'dev' or 'prod'")

    handler = logging.StreamHandler(sys.stdout)
    if environment == PROD:
        handler.setFormatter(JsonFormatter())
        level = logging.INFO
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        level = logging.DEBUG

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return environment


def get_logger(name: str) -> logging.Logger:
    """Return an application logger after applying the configured mode."""
    configure_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
