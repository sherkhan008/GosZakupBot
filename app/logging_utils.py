"""Centralized logging configuration and secret redaction.

Ensures GOSZAKUP_API_TOKEN, TELEGRAM_BOT_TOKEN, and common credential
patterns can never appear in console output, log files, or exception
tracebacks -- even if a third-party library (httpx/httpcore) or an
exception message happens to embed a token-bearing URL or header.

Two layers, per spec:
1. Exact configured-secret replacement -- the actual runtime token values
   are substituted with [REDACTED] wherever they appear in a rendered log
   line, including formatted exception tracebacks.
2. Defensive pattern-based sanitization -- common credential shapes
   (Authorization: Bearer ..., bot<token>/, token=..., api_key=...,
   password=..., secret=...) are redacted even if the exact secret value
   was not passed in (e.g. a stale/rotated token, or a third-party token).
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Iterable, Optional, TextIO

REDACTED = "[REDACTED]"

# Layer 2: defensive pattern-based sanitization. Applied after exact-secret
# replacement. Case-insensitive; covers header/query-param/URL shapes.
_PATTERN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Authorization\s*:\s*Bearer\s+)\S+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE), "Bearer " + REDACTED),
    (re.compile(r"(https?://api\.telegram\.org/bot)[^/\s\"']+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"\btoken\s*=\s*[^\s&\"']+", re.IGNORECASE), "token=" + REDACTED),
    (re.compile(r"\bapi[_-]?key\s*=\s*[^\s&\"']+", re.IGNORECASE), "api_key=" + REDACTED),
    (re.compile(r"\bpassword\s*=\s*[^\s&\"']+", re.IGNORECASE), "password=" + REDACTED),
    (re.compile(r"\bsecret\s*=\s*[^\s&\"']+", re.IGNORECASE), "secret=" + REDACTED),
)

# Secrets shorter than this are too generic to safely blanket-replace
# (e.g. an empty string or a stray short value would nuke unrelated text).
_MIN_SECRET_LENGTH = 6


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact known secret values and common credential patterns from `text`."""
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERN_RULES:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFormatter(logging.Formatter):
    """A logging.Formatter that redacts secrets from the FINAL rendered line
    -- message, interpolated args, and any exception traceback -- so nothing
    slips through regardless of how the record was constructed.
    """

    def __init__(self, fmt: str, secrets: Iterable[str] = ()):
        super().__init__(fmt)
        self._secrets = tuple(dict.fromkeys(s for s in secrets if s))

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_text(rendered, self._secrets)


def configure_logging(
    level: str,
    secrets: Iterable[str] = (),
    *,
    stream: Optional[TextIO] = None,
) -> logging.Handler:
    """Configure root logging with secret redaction and safe defaults.

    Also forces the `httpx`/`httpcore` loggers to WARNING so their default
    INFO-level per-request logging (which includes the full request URL --
    for Telegram, that means the bot token embedded in the path) never
    reaches the console, regardless of the application's own LOG_LEVEL.

    Returns the configured handler (mainly useful for tests).
    """
    target_stream = stream if stream is not None else sys.stdout
    if stream is None:
        for std_stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(std_stream, "reconfigure", None)
            if reconfigure:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(
        SecretRedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets)
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # Third-party libraries whose DEBUG-level instrumentation can leak data we
    # otherwise carefully avoid logging ourselves -- never allow these
    # regardless of the app's own LOG_LEVEL:
    #  - httpx/httpcore log "HTTP Request: POST https://api.telegram.org/bot<TOKEN>/...".
    #  - aiosqlite logs each executed SQL statement's bound parameters at
    #    DEBUG, which would otherwise dump owner_chat_id and tender data.
    for noisy_logger in ("httpx", "httpcore", "aiosqlite"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    return handler
