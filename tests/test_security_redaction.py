"""Security regression tests: secrets and personal data must never appear in
rendered log output, regardless of how the log line was produced (direct
message, %-args, or an exception traceback).
"""
from __future__ import annotations

import io
import logging

import httpx
import pytest

from app.logging_utils import SecretRedactingFormatter, configure_logging, redact_text
from app.telegram.client import TelegramClient, TelegramError

FAKE_TELEGRAM_SECRET = "FAKE_TELEGRAM_SECRET_123456"
FAKE_GOSZAKUP_SECRET = "FAKE_GOSZAKUP_SECRET_987654"
FAKE_CHAT_ID = "FAKE_CHAT_ID_123456789"


@pytest.fixture(autouse=True)
def _restore_root_logging_state():
    """configure_logging() mutates global logging state; snapshot and restore
    it around every test in this module so we don't leak configuration into
    other tests (or pytest's own log capture).
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    httpx_level = logging.getLogger("httpx").level
    httpcore_level = logging.getLogger("httpcore").level
    try:
        yield
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        logging.getLogger("httpx").setLevel(httpx_level)
        logging.getLogger("httpcore").setLevel(httpcore_level)


def _capture_logs(secrets: list[str]) -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    configure_logging("DEBUG", secrets, stream=stream)
    return stream, logging.getLogger("test")


# --------------------------------------------------------------------------- redact_text() unit behavior


def test_redact_text_replaces_exact_secret():
    text = f"token in url: https://api.telegram.org/bot{FAKE_TELEGRAM_SECRET}/getMe"
    redacted = redact_text(text, [FAKE_TELEGRAM_SECRET])
    assert FAKE_TELEGRAM_SECRET not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_pattern_bearer():
    text = "Authorization: Bearer abc123.def456-ghi"
    redacted = redact_text(text)
    assert "abc123.def456-ghi" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_pattern_bot_url_without_exact_secret():
    # Even if the exact runtime secret wasn't passed in, the URL shape alone
    # must be scrubbed (defense in depth for rotated/unknown tokens).
    text = "GET https://api.telegram.org/bot999999:UNKNOWNTOKEN/getUpdates"
    redacted = redact_text(text, secrets=[])
    assert "UNKNOWNTOKEN" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.parametrize(
    "raw,expected_hidden",
    [
        ("token=abcdef123456", "abcdef123456"),
        ("api_key=abcdef123456", "abcdef123456"),
        ("password=hunter2hunter", "hunter2hunter"),
        ("secret=topsecretvalue", "topsecretvalue"),
    ],
)
def test_redact_text_pattern_credential_params(raw: str, expected_hidden: str):
    redacted = redact_text(raw)
    assert expected_hidden not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_leaves_unrelated_text_untouched():
    text = "Incremental sync: 42 lot(s) changed"
    assert redact_text(text, [FAKE_TELEGRAM_SECRET]) == text


# --------------------------------------------------------------------------- Formatter-level (incl. tracebacks)


def test_formatter_redacts_plain_message():
    stream, log = _capture_logs([FAKE_TELEGRAM_SECRET])
    log.info("Using token %s for auth", FAKE_TELEGRAM_SECRET)
    output = stream.getvalue()
    assert FAKE_TELEGRAM_SECRET not in output
    assert "[REDACTED]" in output


def test_formatter_redacts_exception_traceback():
    stream, log = _capture_logs([FAKE_TELEGRAM_SECRET])
    try:
        raise RuntimeError(f"failed calling https://api.telegram.org/bot{FAKE_TELEGRAM_SECRET}/getMe")
    except RuntimeError:
        log.exception("Telegram request blew up")
    output = stream.getvalue()
    assert FAKE_TELEGRAM_SECRET not in output
    assert "[REDACTED]" in output
    assert "Telegram request blew up" in output  # legitimate context is preserved


def test_formatter_redacts_multiple_distinct_secrets_simultaneously():
    stream, log = _capture_logs([FAKE_TELEGRAM_SECRET, FAKE_GOSZAKUP_SECRET])
    log.warning(
        "telegram=%s goszakup=%s", FAKE_TELEGRAM_SECRET, FAKE_GOSZAKUP_SECRET
    )
    output = stream.getvalue()
    assert FAKE_TELEGRAM_SECRET not in output
    assert FAKE_GOSZAKUP_SECRET not in output


def test_configure_logging_forces_httpx_and_httpcore_to_warning():
    _capture_logs([FAKE_TELEGRAM_SECRET])
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# --------------------------------------------------------------------------- end-to-end client behavior


class _RaisingTransport(httpx.AsyncBaseTransport):
    """Simulates the worst case: an exception whose message embeds the full
    token-bearing URL, exactly like some httpx internals can produce.
    """

    def __init__(self, message: str):
        self._message = message

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(self._message)


async def test_telegram_client_error_never_leaks_token_through_logs():
    """Worst case: the underlying transport error message embeds the full
    token-bearing URL (as some httpx internals can). Even if that raw string
    reaches a logger.exception() call somewhere downstream, our redacting
    formatter must still scrub it because the real token was registered as a
    secret to redact.
    """
    stream, log = _capture_logs([FAKE_TELEGRAM_SECRET])

    bad_url = f"https://api.telegram.org/bot{FAKE_TELEGRAM_SECRET}/getMe"
    client = TelegramClient("https://api.telegram.org", FAKE_TELEGRAM_SECRET, timeout=1.0)
    client._client = httpx.AsyncClient(transport=_RaisingTransport(f"Connection to {bad_url} failed"))

    # Patch retry delays down so the test doesn't take minutes.
    import app.telegram.client as telegram_client_module

    original_delays = telegram_client_module.RETRY_DELAYS
    telegram_client_module.RETRY_DELAYS = (0, 0)
    try:
        try:
            await client.get_me()
            assert False, "expected TelegramError"
        except TelegramError:
            log.exception("Telegram getMe failed in test harness")
    finally:
        telegram_client_module.RETRY_DELAYS = original_delays

    await client.close()
    output = stream.getvalue()
    assert FAKE_TELEGRAM_SECRET not in output
    assert bad_url not in output
    assert "[REDACTED]" in output


async def test_pairing_does_not_log_chat_id(monkeypatch):
    from pathlib import Path

    from app.database.db import init_db
    from app.database.repository import Repository
    from app.telegram import pairing as pairing_module

    stream, _ = _capture_logs([])
    pairing_logger = logging.getLogger("app.telegram.pairing")
    pairing_logger.setLevel(logging.DEBUG)

    monkeypatch.setattr(pairing_module, "POLL_INTERVAL_SECONDS", 0)

    class FakeTelegramClient:
        def __init__(self, updates):
            self._updates = updates
            self.sent = []

        async def get_updates(self, offset=None, timeout=0):
            if self._updates:
                return self._updates.pop(0)
            return []

        async def send_message(self, chat_id, text, parse_mode="HTML"):
            self.sent.append((chat_id, text))
            return {"message_id": 1}

    conn = await init_db(Path(":memory:"))
    repo = Repository(conn)
    fake = FakeTelegramClient(
        [[], [{"update_id": 1, "message": {"chat": {"id": FAKE_CHAT_ID, "type": "private"}, "text": "/start"}}]]
    )

    chat_id = await pairing_module.pair_owner(fake, repo)
    assert chat_id == FAKE_CHAT_ID

    output = stream.getvalue()
    assert FAKE_CHAT_ID not in output
    await conn.close()
