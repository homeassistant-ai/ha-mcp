"""Enforce mode refuses config-file reads of a file that defines a hidden item.

``scripts.yaml`` is keyed by storage key and ``scenes.yaml`` / ``automations.yaml``
by config ``id``, so a hidden body never spells out its entity_id. The decision
is made on the whole file: the component applies ``tail_lines`` and
``yaml_path`` itself, and a slice can start anywhere inside a hidden block.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION, FilesystemTools
from ha_mcp.tools.tools_yaml_read import YamlReadTools

# Autouse: each test starts with an empty per-client caller-token cache.
from ..test_caller_token_auth import _reset_token_cache_between_tests  # noqa: F401
from .test_enforcement import FakeClient, _error_body, make_context, make_mw

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

_HIDDEN = [
    ("script.unlock_house", "unlock_house", "script"),
    ("script.front_door", "come_home", "script"),
    ("script.lobby", "other_key", "script"),
    ("scene.den", "1727266720448", "homeassistant"),
]


class _ComponentClient(FakeClient):
    """Registry double plus the component's ``read_file`` service."""

    async def get_services(self) -> list[dict[str, Any]]:
        services = {"get_caller_token": {}, "read_file": {}, "list_files": {}}
        return [{"domain": "ha_mcp_tools", "services": services}]

    async def call_service(
        self, domain: str, service: str, payload: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        if service == "get_caller_token":
            body = {"success": True, "token": "t", "version": MIN_COMPONENT_VERSION}
            return {"service_response": body}
        assert service == "read_file", service
        return {"service_response": _read_file(payload)}


def _read_file(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve the path and apply ``tail_lines`` / ``yaml_path`` like the component."""
    content = _FILES[posixpath.normpath(payload["path"])]
    tail_lines = payload.get("tail_lines")
    if tail_lines:
        content = "".join(content.splitlines(keepends=True)[-tail_lines:])
    body: dict[str, Any] = {"success": True, "path": payload["path"]}
    yaml_path = payload.get("yaml_path")
    if yaml_path is None:
        body["content"] = content
        return body
    node: Any = yaml.safe_load(content)
    for part in yaml_path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    body["subtree"] = None if node is None else yaml.safe_dump(node)
    if payload.get("include_parsed"):
        body["parsed"] = node
    return body


@pytest.fixture
def client(set_config: Callable[..., Any]) -> _ComponentClient:
    set_config(
        enabled=True,
        enforce=True,
        deny_entity_ids=[entity_id for entity_id, _key, _platform in _HIDDEN],
    )
    registry = [
        {"entity_id": entity_id, "unique_id": key, "platform": platform}
        for entity_id, key, platform in _HIDDEN
    ]
    return _ComponentClient(registry={"success": True, "result": registry})


async def _read_file_tool(client: Any, **kwargs: Any) -> Any:
    return await FilesystemTools(client).ha_read_file(**kwargs)


async def _get_yaml_tool(client: Any, **kwargs: Any) -> Any:
    return await YamlReadTools(client).ha_config_get_yaml(**kwargs)


_TOOL_NAMES = {_read_file_tool: "ha_read_file", _get_yaml_tool: "ha_config_get_yaml"}


async def _call_through_enforcement(
    client: _ComponentClient, call: Callable[..., Any], kwargs: dict[str, Any]
) -> Any:
    """Run the real tool behind the enforcement middleware pair."""
    mw = make_mw(get_client=lambda: client)

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
    client: _ComponentClient, call: Callable[..., Any], kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as exc:
        await _call_through_enforcement(client, call, kwargs)
    assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


@pytest.mark.parametrize(
    ("call", "kwargs"),
    [
        pytest.param(
            _read_file_tool,
            {"path": "www/presence.yaml"},
            id="helper-sharing-a-hidden-script-key",
        ),
        pytest.param(
            _get_yaml_tool,
            {"file": "packages/presence.yaml", "yaml_path": "input_boolean.come_home"},
            id="package-helper-sharing-a-hidden-script-key",
        ),
    ],
)
async def test_file_that_only_shares_a_hidden_key_stays_readable(
    client: _ComponentClient, call: Callable[..., Any], kwargs: dict[str, Any]
) -> None:
    result = await _call_through_enforcement(client, call, kwargs)
    assert result.structured_content["success"] is True
