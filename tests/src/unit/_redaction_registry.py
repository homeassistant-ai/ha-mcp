"""An empty known-secret registry for each test of a module that imports it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ha_mcp.redaction import clear_known_secret_values


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Isolate the module-level known-secrets set per test."""
    clear_known_secret_values()
    yield
    clear_known_secret_values()
