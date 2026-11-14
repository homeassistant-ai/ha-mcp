"""Visibility settings for the visibility filter tests."""

from __future__ import annotations

from typing import Any

from ha_mcp.visibility.model import VisibilityConfig


def visibility_config(**kw: Any) -> VisibilityConfig:
    """An enabled config with the default category exclude cleared, so each
    scenario activates exactly the one dimension it names."""
    kw.setdefault("exclude_categories", [])
    return VisibilityConfig(enabled=True, **kw)
