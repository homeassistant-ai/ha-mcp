"""Server-text tools are never gated by entity visibility enforce mode (#2949).

``ha_get_skill_guide``, ``ha_search_tools`` and the ``ha_dev_*`` tools return
bundled docs, tool schemas and server controls, whose examples can name
entity_ids that collide with a user's hidden set.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.visibility import resolver
from ha_mcp.visibility.enforcement import (
    VisibilityInboundEnforcement,
    VisibilityOutboundEnforcement,
)
from ha_mcp.visibility.model import VisibilityConfig

_TEXT = "action: light.turn_on\ntarget:\n  entity_id: light.kitchen"
_GUIDE_ARGS = {"file": "references/automation-actions.md"}


def make_context(name: str, arguments: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=arguments or {})
    )


def text_result(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        structured_content=None,
    )


def _returns(result: SimpleNamespace) -> Callable[[Any], Awaitable[SimpleNamespace]]:
    async def _call_next(context: Any) -> SimpleNamespace:
        return result

    return _call_next


class _ChainedEnforcement:
    """Inbound wrapping outbound, as the server registers the pair."""

    def __init__(self, client: HomeAssistantClient) -> None:
        self.inbound = VisibilityInboundEnforcement(get_client=lambda: client)
        self.outbound = VisibilityOutboundEnforcement(get_client=lambda: client)

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        async def _inner(ctx: Any) -> Any:
            return await self.outbound.on_call_tool(ctx, call_next)

        return await self.inbound.on_call_tool(context, _inner)


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

    result = await _ChainedEnforcement(core_client).on_call_tool(
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

    result = await _ChainedEnforcement(core_client).on_call_tool(
        make_context("ha_get_skill_guide", _GUIDE_ARGS), _returns(text_result(_TEXT))
    )

    assert result.content[0].text == _TEXT
