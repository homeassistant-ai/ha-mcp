"""The component's extra YAML key store is read once the component supports it."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools import yaml_extra_keys

STORE = {"service_response": {"success": True, "keys": ["beta"]}}


@pytest.mark.asyncio
async def test_store_keys_apply_after_the_component_was_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version cached before the update must not drop store-only keys until
    the server restarts; no other check would report them missing."""
    client = MagicMock()
    yaml_extra_keys._COMPONENT_VERSION_CACHE[client] = "1.2.3"

    async def bootstrap(_client: Any, force_refresh: bool = False) -> str:
        if force_refresh:
            yaml_extra_keys._COMPONENT_VERSION_CACHE[client] = (
                yaml_extra_keys.MIN_COMPONENT_VERSION_EXTRA_YAML_KEYS
            )
        return "token"

    monkeypatch.setattr(yaml_extra_keys, "_ensure_caller_token", bootstrap)
    monkeypatch.setattr(
        yaml_extra_keys, "call_mcp_tools_service", AsyncMock(return_value=STORE)
    )

    keys = await yaml_extra_keys.effective_extra_yaml_write_keys(
        client, SimpleNamespace(extra_yaml_write_keys="")
    )

    assert keys == ["beta"]
