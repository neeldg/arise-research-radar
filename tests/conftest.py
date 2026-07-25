"""Shared pytest fixtures. No test in this suite may perform a live network call."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from arise_radar.sinks.notion import NotionClient
from arise_radar.sources.openalex import OpenAlexClient


@pytest.fixture
def mock_openalex_client() -> Callable[..., OpenAlexClient]:
    """Factory for an OpenAlexClient backed by httpx.MockTransport."""

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
    ) -> OpenAlexClient:
        transport = httpx.MockTransport(handler)
        http_client = httpx.Client(transport=transport, base_url="https://api.openalex.org")
        kwargs.setdefault("backoff_seconds", 0)
        return OpenAlexClient(http_client=http_client, **kwargs)

    return _factory


@pytest.fixture
def mock_notion_client() -> Callable[..., NotionClient]:
    """Factory for a NotionClient backed by httpx.MockTransport."""

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
    ) -> NotionClient:
        transport = httpx.MockTransport(handler)
        http_client = httpx.Client(transport=transport, base_url="https://api.notion.com")
        kwargs.setdefault("backoff_seconds", 0)
        return NotionClient(http_client=http_client, **kwargs)

    return _factory
