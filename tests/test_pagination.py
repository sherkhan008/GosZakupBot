"""Regression tests for GosZakup V3 pagination.

Verified live against the real API (2026-08-10): Lots are returned in
DESCENDING id order, and `after=X` means "continue with id < X". A cursor
implementation that advances using the MAXIMUM id seen per page (an
ascending-order assumption) barely moves forward each request and turns
pagination into a near-infinite crawl. These tests pin the correct
(descending, min-id-cursor) behavior against a mock transport that mirrors
what the live API actually does.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.goszakup.client import GoszakupClient


class DescendingOrderTransport(httpx.AsyncBaseTransport):
    """Mimics the live GosZakup API: given a full ordered (descending) id
    list, returns `limit` rows with id < `after` (or from the top if
    after is None), same as the real service does.
    """

    def __init__(self, all_ids_desc: list[int]):
        self.all_ids_desc = all_ids_desc
        self.requests: list[int | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        after = variables.get("after")
        limit = variables["limit"]
        self.requests.append(after)

        if after is None:
            candidates = self.all_ids_desc
        else:
            candidates = [i for i in self.all_ids_desc if i < after]

        page_ids = candidates[:limit]
        lots = [{"id": i, "nameRu": f"Lot {i}"} for i in page_ids]
        return httpx.Response(200, json={"data": {"Lots": lots}})


class StuckCursorTransport(httpx.AsyncBaseTransport):
    """A misbehaving server that always returns the same page, regardless of
    `after` -- used to verify the infinite-loop guard actually stops.
    """

    def __init__(self, ids: list[int]):
        self.ids = ids
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        if self.request_count > 20:
            # Safety net for the test itself, in case the guard regresses.
            return httpx.Response(200, json={"data": {"Lots": []}})
        lots = [{"id": i, "nameRu": f"Lot {i}"} for i in self.ids]
        return httpx.Response(200, json={"data": {"Lots": lots}})


async def test_pagination_walks_all_pages_in_descending_order():
    all_ids = list(range(1000, 700, -1))  # 300 ids, descending: 1000..701
    transport = DescendingOrderTransport(all_ids)
    client = GoszakupClient("https://ows.goszakup.gov.kz/v3/graphql", "test-token")
    client._client = httpx.AsyncClient(transport=transport)

    collected = [raw["id"] async for raw in client.fetch_lots_paginated({}, limit=100)]

    assert collected == all_ids  # every id, in order, no gaps or dupes
    # 300 ids / 100 per page = 3 full pages, plus one more request that comes
    # back empty (a full-length page can't be distinguished from a final page
    # without trying one more time).
    assert len(transport.requests) == 4
    await client.close()


async def test_pagination_cursor_uses_min_id_not_max():
    all_ids = list(range(500, 400, -1))  # 100 ids, descending: 500..401
    transport = DescendingOrderTransport(all_ids)
    client = GoszakupClient("https://ows.goszakup.gov.kz/v3/graphql", "test-token")
    client._client = httpx.AsyncClient(transport=transport)

    collected = [raw["id"] async for raw in client.fetch_lots_paginated({}, limit=40)]

    assert collected == all_ids
    # after values requested: None, then min(page1)=461, then min(page2)=421
    assert transport.requests == [None, 461, 421]
    await client.close()


async def test_pagination_stops_on_stuck_cursor_instead_of_looping_forever():
    # A FULL page (length == limit) so "short final page" never triggers a
    # normal stop -- only the stall guard can end this loop. The transport
    # ignores `after` entirely and always returns the same 10 rows, exactly
    # like a server that failed to apply the cursor.
    stuck_ids = list(range(110, 100, -1))  # 10 ids
    transport = StuckCursorTransport(stuck_ids)
    client = GoszakupClient("https://ows.goszakup.gov.kz/v3/graphql", "test-token")
    client._client = httpx.AsyncClient(transport=transport)

    collected = []
    async for raw in client.fetch_lots_paginated({}, limit=10):
        collected.append(raw["id"])
        if len(collected) > 50:
            pytest.fail("pagination did not stop -- infinite loop guard failed")

    assert collected == stuck_ids  # first page's rows, yielded once
    assert transport.request_count == 2  # initial page + one repeat before the guard fires
    await client.close()


async def test_pagination_empty_result_stops_immediately():
    transport = DescendingOrderTransport([])
    client = GoszakupClient("https://ows.goszakup.gov.kz/v3/graphql", "test-token")
    client._client = httpx.AsyncClient(transport=transport)

    collected = [raw async for raw in client.fetch_lots_paginated({}, limit=50)]
    assert collected == []
    assert len(transport.requests) == 1
    await client.close()
