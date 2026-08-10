"""Minimal async Telegram Bot API client (httpx-based, no framework).

Never logs the bot token.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

RETRY_DELAYS = (1, 2, 4, 8, 16, 30, 60)
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, api_base: str, bot_token: str, timeout: float = 30.0):
        self._base_url = f"{api_base}/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TelegramClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        # The full URL embeds the bot token (https://api.telegram.org/bot<TOKEN>/<method>)
        # and must NEVER be logged. Only the method name (e.g. "getMe",
        # "sendMessage") is safe to log.
        url = f"{self._base_url}/{method}"
        last_exc: Optional[Exception] = None

        for attempt, delay in enumerate((0,) + RETRY_DELAYS):
            if delay:
                logger.warning("Retrying Telegram %s in %ss (attempt %s)", method, delay, attempt)
                await asyncio.sleep(delay)
            try:
                logger.debug("Telegram API request: %s", method)
                response = await self._client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                logger.warning("Telegram network error on %s: %s", method, type(exc).__name__)
                continue

            if response.status_code == 429:
                try:
                    body = response.json()
                    retry_after = body.get("parameters", {}).get("retry_after", 1)
                except ValueError:
                    retry_after = 1
                logger.warning("Telegram 429, honoring retry_after=%ss", retry_after)
                await asyncio.sleep(float(retry_after))
                last_exc = TelegramError("HTTP 429")
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                logger.warning("Telegram HTTP %s (retryable) on %s", response.status_code, method)
                last_exc = TelegramError(f"HTTP {response.status_code}")
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise TelegramError(f"Malformed JSON from Telegram: {exc}") from exc

            if response.status_code != 200 or not body.get("ok", False):
                description = body.get("description", "unknown error")
                raise TelegramError(f"Telegram API error on {method}: {description}")

            logger.debug("Telegram API request completed: %s status=%s", method, response.status_code)
            return body.get("result", {})

        raise TelegramError(f"Telegram {method} failed after retries: {last_exc}") from last_exc

    async def get_me(self) -> dict[str, Any]:
        return await self._post("getMe", {})

    async def get_updates(
        self, offset: Optional[int] = None, timeout: int = 0
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._post("getUpdates", payload)
        return result if isinstance(result, list) else []

    async def send_message(
        self, chat_id: str | int, text: str, parse_mode: str = "HTML"
    ) -> dict[str, Any]:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }
        return await self._post("sendMessage", payload)
