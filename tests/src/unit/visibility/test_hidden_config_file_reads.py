"""Enforce mode refuses config-file reads of a file that defines a hidden item.

``scripts.yaml`` is keyed by storage key and ``scenes.yaml`` / ``automations.yaml``
by config ``id``, so a hidden body never spells out its entity_id. The decision
is made on the whole file: the component applies ``tail_lines`` and
``yaml_path`` itself, and a slice can start anywhere inside a hidden block.

The files sit in Core's config directory and are read by the File & YAML
Tools entry's own ``read_file`` service; the hidden items are real registry
rows.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_filesystem import FilesystemTools
from ha_mcp.tools.tools_yaml_read import YamlReadTools
from ha_mcp.visibility.model import VisibilityConfig

from .._visibility_core import enforce_hiding, error_code, register_entity
from ._chained_enforcement import _ChainedEnforcement, make_context

_SCRIPTS_YAML = """\
unlock_house:
  alias: Unlock house
  sequence:
  - action: lock.unlock
come_home:
  alias: Front door
  sequence: []
other_key:
  alias: other_key
  sequence:
  - action: switch.turn_on
"""

# An unquoted numeric id loads as an int.
_SCENES_YAML = """\
- id: 1727266720448
  name: Den
  entities:
    light.den: 'on'
"""

_PRESENCE_YAML = """\
input_boolean:
  come_home:
    name: Coming home
"""

_DOOR_PACKAGE_YAML = """\
script:
  come_home:
    sequence: []
"""

_FILES = {
    "scripts.yaml": _SCRIPTS_YAML,
    "scenes.yaml": _SCENES_YAML,
    "www/presence.yaml": _PRESENCE_YAML,
    "packages/presence.yaml": _PRESENCE_YAML,
    "packages/door.yaml": _DOOR_PACKAGE_YAML,
}

# (entity_id, registry unique_id, registry platform)
_HIDDEN = [
    ("script.unlock_house", "unlock_house", "script"),
    ("script.front_door", "come_home", "script"),
    ("script.lobby", "other_key", "script"),
    ("scene.den", "1727266720448", "homeassistant"),
]


@pytest.fixture
async def client(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    component_core_client: HomeAssistantClient,
    set_config: Callable[..., VisibilityConfig],
    tmp_path: Path,
) -> HomeAssistantClient:
    """The server's client on a ``hass`` whose config dir holds ``_FILES``."""
    for rel_path, content in _FILES.items():
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    hass.config.config_dir = str(tmp_path)
    # ``config`` serves the registry list the hidden set is computed from.
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert entry.state is ConfigEntryState.LOADED
    for entity_id, unique_id, platform in _HIDDEN:
        register_entity(hass, entity_registry, entity_id, unique_id, platform)
    enforce_hiding(set_config, *(entity_id for entity_id, _key, _platform in _HIDDEN))
    return component_core_client


async def _read_file_tool(client: HomeAssistantClient, **kwargs: Any) -> Any:
    return await FilesystemTools(client).ha_read_file(**kwargs)


async def _get_yaml_tool(client: HomeAssistantClient, **kwargs: Any) -> Any:
    return await YamlReadTools(client).ha_config_get_yaml(**kwargs)


_TOOL_NAMES = {_read_file_tool: "ha_read_file", _get_yaml_tool: "ha_config_get_yaml"}


async def _call_through_enforcement(
    client: HomeAssistantClient, call: Callable[..., Any], kwargs: dict[str, Any]
) -> Any:
    """Run the real tool behind the enforcement middleware pair."""
    mw = _ChainedEnforcement(lambda: client)

    async def _run_tool(_context: Any) -> Any:
        result = await call(client, **kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(result))],
            structured_content=result,
        )

    return await mw.on_call_tool(make_context(_TOOL_NAMES[call], kwargs), _run_tool)


@pytest.mark.parametrize(
    ("call", "kwargs"),
    [
        pytest.param(
            _get_yaml_tool,
            {"file": "scripts.yaml", "yaml_path": "unlock_house"},
            id="yaml-path-to-hidden-script",
        ),
        pytest.param(
            _get_yaml_tool,
            {
                "file": "scripts.yaml",
                "yaml_path": "other_key.sequence",
                "include_parsed": True,
                "include_content": False,
            },
            id="parsed-yaml-path-inside-renamed-hidden-script",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "scripts.yaml", "tail_lines": 3},
            id="tail-below-hidden-script-key",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "scenes.yaml", "tail_lines": 3},
            id="tail-below-hidden-scene-id",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "packages/door.yaml"},
            id="package-defining-hidden-script",
        ),
        # The component normalizes the path before reading it.
        pytest.param(
            _read_file_tool,
            {"path": ".//scripts.yaml", "tail_lines": 3},
            id="scripts-yaml-spelled-with-doubled-separator",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "www/../scripts.yaml", "tail_lines": 3},
            id="scripts-yaml-spelled-through-parent-segment",
        ),
    ],
)
async def test_read_of_a_file_defining_a_hidden_item_is_refused(
    client: HomeAssistantClient, call: Callable[..., Any], kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(client, call, kwargs)
    assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


@pytest.mark.parametrize(
    ("call", "kwargs", "expected"),
    [
        pytest.param(
            _read_file_tool,
            {"path": "www/presence.yaml"},
            "Coming home",
            id="helper-sharing-a-hidden-script-key",
        ),
        pytest.param(
            _get_yaml_tool,
            {"file": "packages/presence.yaml", "yaml_path": "input_boolean.come_home"},
            "Coming home",
            id="package-helper-sharing-a-hidden-script-key",
        ),
    ],
)
async def test_file_that_only_shares_a_hidden_key_stays_readable(
    client: HomeAssistantClient,
    call: Callable[..., Any],
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    result = await _call_through_enforcement(client, call, kwargs)
    assert result.structured_content["success"] is True
    assert expected in result.content[0].text
