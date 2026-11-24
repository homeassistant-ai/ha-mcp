"""Enforce mode refuses config-file reads of a file that defines a hidden item.

``scripts.yaml`` is keyed by storage key and ``scenes.yaml`` / ``automations.yaml``
by config ``id``, so a hidden body never spells out its entity_id. Which file
defines an item is what Core's loader reads it from, ``!include``, ``!secret``
and packages included. The decision is made on the whole file: the component
applies ``tail_lines`` and ``yaml_path`` itself, and a slice can start anywhere
inside a hidden block.

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.config_file_items import WS_CONFIG_FILE_ITEMS
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from custom_components.ha_mcp_tools.websocket_api import system
from custom_components.ha_mcp_tools.websocket_api.constants import WS_INFO
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_file_visibility import refuse_reading_hidden_definitions
from ha_mcp.tools.tools_filesystem import FilesystemTools
from ha_mcp.tools.tools_yaml_read import YamlReadTools
from ha_mcp.visibility import persistence, resolver
from ha_mcp.visibility.model import VisibilityConfig

from .._command_faults import before_handling, dropped_on, pooled_socket
from .._symlink_support import symlink_or_skip
from .._tool_errors import error_body
from .._visibility_core import enforce_hiding, error_code, register_entity
from ._chained_enforcement import _ChainedEnforcement, make_context

_CONFIGURATION_YAML = """\
homeassistant:
  packages: !include_dir_named packages
script: !include scripts.yaml
script named: !include_dir_named scripts
scene: !include scenes.yaml
automation split: !include my_autos.yaml
automation merged: !include_dir_merge_list automations_merged
automation listed: !include_dir_list automations_listed
script merged: !include_dir_merge_named scripts_merged
automation single: !include single.yaml
automation plain:
- alias: No id
  triggers: []
  actions: []
"""

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
  entities: !include den.yaml
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

# A hidden automation whose actions are in a file of their own.
_MY_AUTOS_YAML = """\
- id: "1700000000001"
  alias: Garage
  triggers: []
  actions: !include common.yaml
"""

_FILES = {
    "configuration.yaml": _CONFIGURATION_YAML,
    "scripts.yaml": _SCRIPTS_YAML,
    "scripts/secret_door.yaml": "alias: !secret door_name\nsequence: []\n",
    "secrets.yaml": "door_name: Secret door\n",
    # Only keys carry this file's name: ``on`` loads as true.
    "den.yaml": "light.den: on\n",
    "home-assistant.log.fault": "Fatal Python error: Segmentation fault\n",
    "my_autos.yaml": _MY_AUTOS_YAML,
    "common.yaml": "- action: light.turn_on\n",
    "automations_merged/garage.yaml": (
        '- id: "1700000000002"\n  triggers: []\n  actions: []\n'
    ),
    "automations_listed/garage.yaml": (
        'id: "1700000000003"\ntriggers: []\nactions: []\n'
    ),
    "scripts_merged/garage.yaml": "lock_garage:\n  sequence: []\n",
    # A section holding one automation rather than a list.
    "single.yaml": 'id: "1700000000004"\ntriggers: []\nactions: []\n',
    # Core loads neither: copies of hidden definitions define nothing.
    "www/scripts.yaml": _SCRIPTS_YAML,
    "www/my_autos.yaml": _MY_AUTOS_YAML,
    "scenes.yaml": _SCENES_YAML,
    "www/presence.yaml": _PRESENCE_YAML,
    "packages/presence.yaml": _PRESENCE_YAML,
    # Sorts before the hidden package, so a glob read targets it first.
    "packages/aaa.yaml": "input_text:\n  note:\n    name: Note\n",
    "packages/door.yaml": _DOOR_PACKAGE_YAML,
}

# (entity_id, registry unique_id, registry platform)
_HIDDEN = [
    ("script.unlock_house", "unlock_house", "script"),
    ("script.front_door", "come_home", "script"),
    ("script.lobby", "other_key", "script"),
    ("scene.den", "1727266720448", "homeassistant"),
    ("script.secret_door", "secret_door", "script"),
    ("automation.garage", "1700000000001", "automation"),
    ("automation.garage_merged", "1700000000002", "automation"),
    ("automation.garage_listed", "1700000000003", "automation"),
    ("script.lock_garage", "lock_garage", "script"),
    ("automation.garage_single", "1700000000004", "automation"),
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
        pytest.param(
            _read_file_tool,
            {"path": "my_autos.yaml"},
            id="automation-in-an-included-file",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "scripts/secret_door.yaml"},
            id="script-in-an-include-dir-named-file",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "common.yaml"},
            id="actions-included-into-a-hidden-automation",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "automations_merged/garage.yaml"},
            id="automation-in-an-include-dir-merge-list-file",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "automations_listed/garage.yaml"},
            id="automation-in-an-include-dir-list-file",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "scripts_merged/garage.yaml"},
            id="script-in-an-include-dir-merge-named-file",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "den.yaml"},
            id="mapping-without-text-included-into-a-hidden-scene",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "single.yaml"},
            id="automation-section-of-one-mapping",
        ),
        pytest.param(
            _get_yaml_tool,
            {"file": "packages/*.yaml", "yaml_path": "script"},
            id="glob-whose-later-target-defines-a-hidden-script",
        ),
    ],
)
async def test_read_of_a_file_defining_a_hidden_item_is_refused(
    client: HomeAssistantClient, call: Callable[..., Any], kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(client, call, kwargs)
    assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


async def test_a_link_to_a_file_defining_a_hidden_item_is_refused(
    client: HomeAssistantClient, tmp_path: Path
) -> None:
    symlink_or_skip(tmp_path / "www" / "link.yaml", tmp_path / "scripts.yaml")

    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(
            client, _read_file_tool, {"path": "www/link.yaml"}
        )
    assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


async def test_a_config_dir_reached_through_a_link_is_checked(
    client: HomeAssistantClient, hass: HomeAssistant, tmp_path: Path
) -> None:
    """Core's loader names each file through the link, as the config dir
    was given; both its names and the requested path are compared resolved."""
    link = tmp_path.parent / f"{tmp_path.name}-link"
    symlink_or_skip(link, tmp_path)
    hass.config.config_dir = str(link)

    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(
            client, _read_file_tool, {"path": "scripts.yaml", "tail_lines": 3}
        )
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
        pytest.param(
            _read_file_tool,
            {"path": "www/scripts.yaml"},
            "Unlock house",
            id="copy-of-scripts-yaml-core-does-not-load",
        ),
        pytest.param(
            _read_file_tool,
            {"path": "www/my_autos.yaml"},
            "Garage",
            id="copy-of-an-included-file-core-does-not-load",
        ),
        # A hidden script's ``!secret`` reads it; its values are masked.
        pytest.param(
            _read_file_tool,
            {"path": "secrets.yaml"},
            "door_name",
            id="secrets-yaml-a-hidden-script-takes-a-secret-from",
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


async def test_a_read_while_the_configuration_does_not_load_is_refused(
    client: HomeAssistantClient, tmp_path: Path
) -> None:
    (tmp_path / "configuration.yaml").write_text("script: !include missing.yaml\n")

    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(
            client, _read_file_tool, {"path": "www/presence.yaml"}
        )
    assert error_code(exc) == "CONFIG_INVALID"
    assert "missing.yaml" in error_body(exc)["error"]["details"]


async def test_a_log_stays_readable_while_the_configuration_does_not_load(
    client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Core writes its logs and never loads them, so they need no check."""
    (tmp_path / "configuration.yaml").write_text("script: !include missing.yaml\n")

    result = await _call_through_enforcement(
        client, _read_file_tool, {"path": "home-assistant.log.fault"}
    )
    assert result.structured_content["success"] is True
    assert "Segmentation fault" in result.content[0].text


async def test_a_component_without_the_check_refuses_the_read(
    client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertised = [c for c in system.CAPABILITIES if c != "config_file_items"]
    monkeypatch.setattr(system, "CAPABILITIES", advertised)

    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(
            client, _read_file_tool, {"path": "www/presence.yaml"}
        )
    assert error_code(exc) == "COMPONENT_NOT_INSTALLED"


async def test_a_read_while_the_component_cannot_be_reached_is_refused(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    socket = await pooled_socket(client)
    with pytest.raises(ToolError) as exc, dropped_on(hass, WS_INFO, socket):
        await _call_through_enforcement(
            client, _read_file_tool, {"path": "www/presence.yaml"}
        )
    assert error_code(exc) == "CONNECTION_FAILED"


async def test_a_read_the_component_cannot_answer_is_refused(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    def fail(msg: dict[str, Any]) -> None:
        raise HomeAssistantError("check failed")

    with (
        pytest.raises(ToolError) as exc,
        before_handling(hass, WS_CONFIG_FILE_ITEMS, fail),
    ):
        await _call_through_enforcement(
            client, _read_file_tool, {"path": "www/presence.yaml"}
        )
    assert error_code(exc) == "SERVICE_CALL_FAILED"


async def _read_scripts_after_the_config_breaks(
    client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:
    """One server's middleware loads the config, then the file breaks."""
    mw = _ChainedEnforcement(lambda: client)

    async def _run(kwargs: dict[str, Any]) -> Any:
        async def _run_tool(_context: Any) -> Any:
            result = await _read_file_tool(client, **kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(result))],
                structured_content=result,
            )

        return await mw.on_call_tool(make_context("ha_read_file", kwargs), _run_tool)

    await _run({"path": "www/presence.yaml"})
    monkeypatch.setattr(
        resolver, "load_visibility_config", persistence.load_visibility_config
    )
    (tmp_path / persistence.VISIBILITY_FILENAME).write_text("{ corrupt")
    return await _run({"path": "scripts.yaml"})


async def test_a_config_breaking_under_enforce_mode_keeps_refusing(
    client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The middleware keeps the last config it loaded; so does the file check."""
    with pytest.raises(ToolError) as exc:
        await _read_scripts_after_the_config_breaks(client, monkeypatch, tmp_path)
    assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


async def test_a_config_breaking_outside_enforce_mode_refuses_nothing(
    client: HomeAssistantClient,
    set_config: Callable[..., VisibilityConfig],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    set_config()

    result = await _read_scripts_after_the_config_breaks(client, monkeypatch, tmp_path)
    assert "Unlock house" in result.content[0].text


async def test_a_config_that_never_loaded_refuses_the_check(
    unreachable_client: HomeAssistantClient,
    set_config: Callable[..., VisibilityConfig],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no good load to fall back on, the check cannot tell enforce mode is off."""
    set_config()
    monkeypatch.setattr(
        resolver, "load_visibility_config", persistence.load_visibility_config
    )
    (tmp_path / persistence.VISIBILITY_FILENAME).write_text("{ corrupt")

    with pytest.raises(ToolError) as exc:
        await refuse_reading_hidden_definitions(
            unreachable_client, "ha_read_file", ["www/presence.yaml"]
        )
    assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


async def test_a_hidden_set_that_cannot_be_loaded_refuses_the_check(
    unreachable_client: HomeAssistantClient,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    enforce_hiding(set_config, _HIDDEN[0][0])

    with pytest.raises(ToolError) as exc:
        await refuse_reading_hidden_definitions(
            unreachable_client, "ha_read_file", ["www/presence.yaml"]
        )
    assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"
