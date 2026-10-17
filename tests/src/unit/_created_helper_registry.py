"""A WebSocket fake for helper creation without the component.

Only the entity registry proves which entity a new storage helper got, so the
create path reads ``config/entity_registry/list`` after Core's create.
"""

from typing import Any
from unittest.mock import AsyncMock

CREATED_ENTRY = {
    "entity_id": "input_boolean.abc123",
    "platform": "input_boolean",
    "unique_id": "abc123",
}


def with_registry(answer: Any) -> AsyncMock:
    """Answer the entity registry list with the created helper's entry and every
    other command with ``answer`` (a reply, or an async handler)."""

    async def send(msg: dict) -> Any:
        if msg.get("type") == "config/entity_registry/list":
            return {"success": True, "result": [CREATED_ENTRY]}
        return await answer(msg) if callable(answer) else answer

    return AsyncMock(side_effect=send)
