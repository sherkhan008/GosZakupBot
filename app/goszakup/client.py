"""Async GosZakup V3 GraphQL client with retries and cursor pagination.

Never logs the Authorization header or the token value.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

import httpx

from app.goszakup.queries import LOTS_QUERY

logger = logging.getLogger(__name__)

RETRY_DELAYS = (1, 2, 4, 8, 16, 30, 60)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_PAGE_LIMIT = 100


class GoszakupError(Exception):
    """Raised for non-retryable GraphQL/API errors (bad query, bad schema field, etc.)."""


class GoszakupClient:
    def __init__(self, api_url: str, api_token: str, timeout: float = 30.0):
        self._api_url = api_url
        self._api_token = api_token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GoszakupClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        return headers

    async def execute(
        self, query: str, variables: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query with exponential-backoff retries on
        transient network/HTTP errors. Returns the 'data' object.
        Raises GoszakupError for GraphQL-level errors (not retried) or after
        retries are exhausted for transient errors.
        """
        payload = {"query": query, "variables": variables or {}}
        last_exc: Optional[Exception] = None

        for attempt, delay in enumerate((0,) + RETRY_DELAYS):
            if delay:
                logger.warning(
                    "Retrying GosZakup request in %ss (attempt %s)", delay, attempt
                )
                await asyncio.sleep(delay)
            try:
                # Safe to log: the GosZakup endpoint URL itself carries no
                # secret (the token only ever goes in the Authorization
                # header, which is never logged).
                logger.debug("GosZakup API request: POST %s", self._api_url)
                response = await self._client.post(
                    self._api_url, json=payload, headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                logger.warning("GosZakup network error: %s", type(exc).__name__)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                retry_after = response.headers.get("Retry-After")
                logger.warning(
                    "GosZakup HTTP %s (retryable)%s",
                    response.status_code,
                    f", retry_after={retry_after}" if retry_after else "",
                )
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                if retry_after:
                    try:
                        await asyncio.sleep(float(retry_after))
                    except ValueError:
                        pass
                continue

            if response.status_code != 200:
                body_preview = response.text[:300]
                raise GoszakupError(
                    f"GosZakup returned HTTP {response.status_code}: {body_preview}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise GoszakupError(f"Malformed JSON from GosZakup: {exc}") from exc

            logger.debug("GosZakup API request completed: status=%s", response.status_code)

            if "errors" in body and body["errors"]:
                # GosZakup's own public error payload (field/type name, message) --
                # does not echo back our Authorization header or request variables.
                raise GoszakupError(f"GosZakup GraphQL errors: {body['errors']}")

            data = body.get("data")
            if data is None:
                raise GoszakupError("GosZakup response missing 'data' field")
            return data

        raise GoszakupError(
            f"GosZakup request failed after retries: {last_exc}"
        ) from last_exc

    async def fetch_lots_page(
        self, filter_dict: dict[str, Any], limit: int, after: Optional[int]
    ) -> list[dict[str, Any]]:
        data = await self.execute(
            LOTS_QUERY, {"filter": filter_dict, "limit": limit, "after": after}
        )
        lots = data.get("Lots") or []
        return lots

    async def fetch_lots_paginated(
        self, filter_dict: dict[str, Any], limit: int = DEFAULT_PAGE_LIMIT
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw lot dicts across all pages until exhausted.

        Pagination follows the documented cursor style (limit/after; this API
        returns a plain list, not Relay-style hasNextPage/pageInfo). Verified
        live against the real V3 API: results come back in DESCENDING Lots.id
        order, and `after=X` returns the next page of rows with id < X (NOT
        id > X). So the cursor for the next page is the MINIMUM id seen on
        the current page, not the maximum -- using the maximum (as a naive
        ascending-order assumption would) barely advances the cursor at all
        and turns pagination into an O(total rows) crawl.

        Guarded against infinite loops: the cursor (min id seen on a page)
        must strictly advance (decrease) on every non-empty page; if it
        doesn't (e.g. the API repeats the same page), pagination is aborted
        and an error is logged rather than looping forever.
        """
        after: Optional[int] = None
        seen_ids: set[int] = set()
        while True:
            page = await self.fetch_lots_page(filter_dict, limit, after)
            if not page:
                break

            new_in_page = 0
            min_id_in_page = after
            for raw_lot in page:
                lot_id = raw_lot.get("id")
                if lot_id is None or lot_id in seen_ids:
                    continue
                seen_ids.add(lot_id)
                new_in_page += 1
                if min_id_in_page is None or lot_id < min_id_in_page:
                    min_id_in_page = lot_id
                yield raw_lot

            if new_in_page == 0 or min_id_in_page == after:
                logger.error(
                    "GosZakup pagination cursor did not advance after %d row(s) on this "
                    "page; stopping to avoid an infinite loop",
                    len(page),
                )
                break

            after = min_id_in_page
            if len(page) < limit:
                break

    async def fetch_lots_by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        results: list[dict[str, Any]] = []
        chunk_size = 100
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            data = await self.execute(
                LOTS_QUERY, {"filter": {"id": chunk}, "limit": chunk_size, "after": None}
            )
            results.extend(data.get("Lots") or [])
        return results
