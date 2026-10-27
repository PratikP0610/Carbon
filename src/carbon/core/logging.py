"""Carbon structured logging configuration."""

import logging
import sys


def configure_logging(level: str = "INFO", log_format: str | None = None) -> None:
    """Configure structured logging for Carbon."""
    numeric_level = getattr(
        logging.LOG_LEVELS.get(level.upper(), logging.INFO)
        if hasattr(logging, "LOG_LEVELS")
        else logging,
        level.upper(),
        logging.INFO,
    )

    if not log_format:
        log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # Remove existing handlers to avoid duplicates
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(numeric_level)

    root.setLevel(numeric_level)
    root.addHandler(console_handler)

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
