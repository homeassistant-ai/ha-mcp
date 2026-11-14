"""``ha_manage_blueprints`` reads a blueprint's body through the component.

Core's ``blueprint/list`` returns metadata only, so ``action="get"`` serves the
parsed body and the file text when the component's ``blueprint_get`` reads the
file, and metadata alone when it cannot: no component, a file it cannot read,
a dropped command or socket. The blueprint is a file Core's ``automation``
integration loads from the test's own config directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import blueprint_sources
from ha_mcp.tools.blueprint_sources import COMPONENT_BODY_WARNING
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.tools.tools_blueprints import BlueprintTools

from ._client_spies import sent_commands
from ._component_frames import pooled

_PATH = "user/motion.yaml"
_BLUEPRINT_GET = "ha_mcp_tools/blueprint_get"
_MOTION = (
    "blueprint:\n"
    "  name: Motion Light\n"
    "  domain: automation\n"
    "  input:\n"
    "    motion_sensor:\n"
    "      name: Motion Sensor\n"
    "triggers:\n"
    "  - trigger: state\n"
    "    entity_id: !input motion_sensor\n"
    "    to: 'on'\n"
    "actions:\n"
    "  - action: light.turn_on\n"
)


def _write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


@pytest.fixture
async def blueprint(hass: HomeAssistant) -> Path:
    """``user/motion.yaml``, installed where Core's automation integration reads it."""
    target = Path(hass.config.path("blueprints", "automation", _PATH))
    await hass.async_add_executor_job(_write, target, _MOTION.encode())
    assert await async_setup_component(hass, "automation", {})
    return target


async def _get(client: HomeAssistantClient) -> dict[str, Any]:
    result: dict[str, Any] = await BlueprintTools(client).ha_manage_blueprints(
        action="get", path=_PATH, domain="automation"
    )
    return result


def _body_warned(result: dict[str, Any]) -> bool:
    return COMPONENT_BODY_WARNING in result.get("warnings", [])


async def _rewritten_after_core_loaded_it(
    hass: HomeAssistant, client: HomeAssistantClient, blueprint: Path, content: bytes
) -> None:
    """Core lists the blueprint, then its file changes: Core keeps listing the
    metadata it loaded until blueprints are reloaded."""
    await BlueprintTools(client).ha_manage_blueprints(
        action="list", domain="automation"
    )
    await hass.async_add_executor_job(_write, blueprint, content)


async def test_get_adds_the_component_body_and_text_to_cores_metadata(
    component_core_client: HomeAssistantClient, blueprint: Path
) -> None:
    socket = await pooled(component_core_client)

    with sent_commands(socket) as sent:
        result = await _get(component_core_client)

    data = result["data"]
    assert data["metadata"]["name"] == "Motion Light"
    assert data["inputs"]["motion_sensor"]["name"] == "Motion Sensor"
    assert data["config"]["triggers"][0]["entity_id"] == {"__input__": "motion_sensor"}
    assert data["config"]["actions"] == [{"action": "light.turn_on"}]
    assert (data["yaml"], data["yaml_source"]) == (_MOTION, "component")
    assert [c.kwargs for c in sent.call_args_list if c.args[0] == _BLUEPRINT_GET] == [
        {"domain": "automation", "path": _PATH}
    ]


async def test_without_the_component_get_serves_metadata_without_a_warning(
    core_client: HomeAssistantClient, blueprint: Path
) -> None:
    """No component: nothing could read the body, and that is no fault to
    report."""
    socket = await pooled(core_client)

    with sent_commands(socket) as sent:
        result = await _get(core_client)

    data = result["data"]
    assert data["metadata"]["name"] == "Motion Light"
    assert "config" not in data
    assert "yaml" not in data
    assert not _body_warned(result)
    assert _BLUEPRINT_GET not in [c.args[0] for c in sent.call_args_list]


async def test_a_file_the_component_cannot_read_is_reported_not_silent(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, blueprint: Path
) -> None:
    """Metadata only, as without the component, but the response says the
    component could not read the body (#1813)."""
    await _rewritten_after_core_loaded_it(
        hass, component_core_client, blueprint, b"blueprint:\n  name: \xff\xfe\n"
    )

    result = await _get(component_core_client)

    assert result["data"]["metadata"]["name"] == "Motion Light"
    assert "config" not in result["data"]
    assert "yaml" not in result["data"]
    assert _body_warned(result)


async def test_text_that_does_not_parse_is_still_returned(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, blueprint: Path
) -> None:
    """The text is what a caller needs to repair the file and save it back."""
    await _rewritten_after_core_loaded_it(
        hass, component_core_client, blueprint, b"blueprint: [unclosed\n"
    )

    result = await _get(component_core_client)

    data = result["data"]
    assert (data["yaml"], data["yaml_source"]) == (
        "blueprint: [unclosed\n",
        "component",
    )
    assert "config" not in data
    assert not _body_warned(result)


async def test_a_command_core_no_longer_knows_drops_the_cached_capabilities(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    blueprint: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component downgraded since the probe: metadata only, and the next
    call probes again instead of asking for the command on stale caps."""
    assert "config" in (await _get(component_core_client))["data"]
    monkeypatch.delitem(hass.data["websocket_api"], _BLUEPRINT_GET)

    with patch.object(
        blueprint_sources, "invalidate_caps", wraps=invalidate_caps
    ) as invalidate:
        result = await _get(component_core_client)

    assert result["data"]["metadata"]["name"] == "Motion Light"
    assert "config" not in result["data"]
    invalidate.assert_called_once_with(component_core_client)


async def test_a_body_read_that_times_out_serves_metadata(
    component_core_client: HomeAssistantClient, blueprint: Path
) -> None:
    with patch.object(
        blueprint_sources,
        "get_websocket_client",
        AsyncMock(side_effect=HomeAssistantCommandTimeout("timeout")),
    ):
        result = await _get(component_core_client)

    assert result["data"]["metadata"]["name"] == "Motion Light"
    assert "config" not in result["data"]


async def test_a_body_read_without_a_socket_serves_metadata(
    component_core_client: HomeAssistantClient, blueprint: Path
) -> None:
    """There is no other full-body read, so a dropped socket leaves the
    metadata already fetched rather than failing the call."""
    with patch.object(
        blueprint_sources,
        "get_websocket_client",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
    ):
        result = await _get(component_core_client)

    assert result["data"]["metadata"]["name"] == "Motion Light"
    assert "config" not in result["data"]


async def test_listing_blueprints_never_asks_the_component(
    component_core_client: HomeAssistantClient, blueprint: Path
) -> None:
    socket = await pooled(component_core_client)

    with sent_commands(socket) as sent:
        result = await BlueprintTools(component_core_client).ha_manage_blueprints(
            action="list", domain="automation"
        )

    assert _PATH in [b["path"] for b in result["data"]["blueprints"]]
    assert not [
        c for c in sent.call_args_list if str(c.args[0]).startswith("ha_mcp_tools/")
    ]
