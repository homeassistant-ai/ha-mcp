"""An empty known-secret registry for each test of a module that imports it,
and the options the app tools show for an app."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from ha_mcp.redaction import clear_known_secret_values, redact_addon_info


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Isolate the module-level known-secrets set per test."""
    clear_known_secret_values()
    yield
    clear_known_secret_values()


def redacted_options(options: Any, schema: Any, slug: str) -> Any:
    """The options the app tools show for an app with ``options`` and ``schema``."""
    addon, _warnings = redact_addon_info({"options": options, "schema": schema}, slug)
    return addon["options"]
