"""Carbon structured logging configuration with sensitive data sanitization."""

import logging
import re
import sys

# Pattern matching sensitive tokens, authorization credentials, and passwords
SENSITIVE_PATTERNS = [
    (re.compile(r"(Bearer\s+)([A-Za-z0-9_\-]{16,})", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"('token':\s*')([^']+)(')", re.IGNORECASE), r"\1[REDACTED_TOKEN]\3"),
    (re.compile(r'("token":\s*")([^"]+)(")', re.IGNORECASE), r"\1[REDACTED_TOKEN]\3"),
    (re.compile(r"('password':\s*')([^']+)(')", re.IGNORECASE), r"\1[REDACTED_CREDENTIAL]\3"),
    (re.compile(r'("password":\s*")([^"]+)(")', re.IGNORECASE), r"\1[REDACTED_CREDENTIAL]\3"),
    (re.compile(r"(token=)([A-Za-z0-9_\-]{16,})", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
]


class TokenSanitizingFilter(logging.Filter):
    """Filter that redacts sensitive bearer tokens and passwords from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            for pattern, repl in SENSITIVE_PATTERNS:
                msg = pattern.sub(repl, msg)
            record.msg = msg
        return True


def configure_logging(level: str = "INFO", log_format: str | None = None) -> None:
    """Configure structured logging for Carbon with automatic token redaction."""
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
    console_handler.addFilter(TokenSanitizingFilter())

    root.setLevel(numeric_level)
    root.addHandler(console_handler)

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
