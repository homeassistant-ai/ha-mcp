"""Thread radio handler for ``ha_manage_radio`` — placeholder pending implementation."""

from __future__ import annotations

from typing import Any

from .base import ActionSpec

SUPPORTED: dict[str, ActionSpec] = {}


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one thread action (validation/confirm applied by the dispatcher)."""
    raise AssertionError(f"unhandled thread action: {action}")
