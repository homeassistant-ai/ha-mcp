"""Server-text tools are never gated by entity visibility enforce mode (#2949).

``ha_get_skill_guide``, ``ha_search_tools`` and the ``ha_dev_*`` tools return
bundled docs, tool schemas and server controls, whose examples can name
entity_ids that collide with a user's hidden set.
"""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig

from ._chained_enforcement import (
    _ChainedEnforcement,
    _returns,
    make_context,
    text_result,
)

_TEXT = "action: light.turn_on\ntarget:\n  entity_id: light.kitchen"
_GUIDE_ARGS = {"file": "references/automation-actions.md"}


@pytest.mark.parametrize(
    "context",
    [
        make_context("ha_get_skill_guide", _GUIDE_ARGS),
        make_context(
            "ha_call_read_tool",
            {"name": "ha_get_skill_guide", "arguments": _GUIDE_ARGS},
        ),
        make_context("ha_search_tools", {"tools": ["ha_call_service"]}),
        make_context("ha_dev_manage_server", {"action": "info"}),
    ],
    ids=["skill_guide", "proxied_skill_guide", "search_tools", "dev_prefix"],
)
async def test_server_text_naming_hidden_entity_is_not_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    set_config: Callable[..., VisibilityConfig],
    context: SimpleNamespace,
) -> None:
    hass.states.async_set("light.kitchen", "on")
    monkeypatch.setattr(
        core_client,
        "get_states",
        AsyncMock(side_effect=HomeAssistantConnectionError("ha down")),
    )
    set_config(enabled=True, enforce=True, deny_entity_ids=["light.kitchen"])

    result = await _ChainedEnforcement(lambda: core_client).on_call_tool(
        context, _returns(text_result(_TEXT))
    )

    assert result.content[0].text == _TEXT
    cast(AsyncMock, core_client.get_states).assert_not_called()


async def test_skill_guide_is_served_when_visibility_config_cannot_load(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _broken(_d: Path) -> VisibilityConfig:
        raise ValueError("entity_visibility.json is not valid JSON")

    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(resolver, "load_visibility_config", _broken)

    result = await _ChainedEnforcement(lambda: core_client).on_call_tool(
        make_context("ha_get_skill_guide", _GUIDE_ARGS), _returns(text_result(_TEXT))
    )

    assert result.content[0].text == _TEXT
