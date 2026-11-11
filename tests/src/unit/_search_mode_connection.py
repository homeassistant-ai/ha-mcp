"""The search-mode catalog connection the categorized-search tests run on."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ha_mcp.tool_catalog import catalog_endpoint


@pytest.fixture(autouse=True)
def search_connection() -> Iterator[None]:
    """These tests exercise the transform on a search-mode connection."""
    with catalog_endpoint("search"):
        yield
