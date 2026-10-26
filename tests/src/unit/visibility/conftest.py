"""Shared fixtures for the entity visibility enforce-mode tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ha_mcp.visibility import enforcement, resolver
from ha_mcp.visibility.model import VisibilityConfig


@pytest.fixture
def set_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., VisibilityConfig]:
    """Install the active VisibilityConfig for both the middleware's active-check
    load and the resolver's internal load; returns a setter to (re)seed it."""
    state = {"config": VisibilityConfig()}
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(resolver, "load_visibility_config", lambda _d: state["config"])

    def _set(**kwargs: Any) -> VisibilityConfig:
        kwargs.setdefault("exclude_categories", [])
        state["config"] = VisibilityConfig(**kwargs)
        return state["config"]

    return _set


@pytest.fixture(autouse=True)
def _fresh_hidden_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-shared hidden-set cache per test.

    The cache is deliberately module-global (a process can hold several server
    instances — see _HiddenSetCache), so without this every test would inherit
    the previous test's cached hidden set.
    """
    monkeypatch.setattr(enforcement, "_hidden_cache", enforcement._HiddenSetCache())
