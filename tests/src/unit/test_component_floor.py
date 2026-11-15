"""The component version the File & YAML Tools entry's services need.

The component and the server ship in lockstep, so the floor is the release's
own version: semantic-release stamps it beside the component's version, and a
component behind it is refused until it is updated.
"""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

import custom_components.ha_mcp_tools as component
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_filesystem

from ._file_services import make_file_service
from ._tool_errors import error_of

_REPO = Path(__file__).resolve().parents[3]
_SOURCE = Path(tools_filesystem.__file__).resolve()


def test_floor_is_the_version_the_component_ships_with() -> None:
    """A floor left at an old release lets an outdated component through
    without the "update" prompt."""
    manifest = json.loads(
        (Path(component.__file__).parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == tools_filesystem.MIN_COMPONENT_VERSION


def test_every_release_stamps_the_floor() -> None:
    """semantic-release rewrites ``NAME = "x.y.z"`` and skips, without an
    error, a declaration it cannot find: the floor stays one plain string
    assignment in the file ``version_variables`` names."""
    release = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["semantic_release"]
    stamped = f"{_SOURCE.relative_to(_REPO).as_posix()}:MIN_COMPONENT_VERSION"
    assert stamped in release["version_variables"]

    assigned = [
        node.value
        for node in ast.parse(_SOURCE.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "MIN_COMPONENT_VERSION"
            for target in node.targets
        )
    ]
    assert len(assigned) == 1
    assert isinstance(assigned[0], ast.Constant)
    assert assigned[0].value == tools_filesystem.MIN_COMPONENT_VERSION


async def test_a_refused_component_works_again_once_it_is_updated(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every call is refused while the component is behind, and the refusal
    is not remembered: the update the error asks for is enough, with no
    restart of the server. Core sets up the File & YAML Tools entry, which
    reports the component's own version; the server's floor is raised above
    it, as on a server one release ahead, and put back, as after the update."""
    await make_file_service(hass)
    released = tools_filesystem.MIN_COMPONENT_VERSION
    ahead = f"{int(released.split('.')[0]) + 1}.0.0"

    with monkeypatch.context() as server_ahead:
        server_ahead.setattr(tools_filesystem, "MIN_COMPONENT_VERSION", ahead)
        for _ in range(2):
            with pytest.raises(ToolError) as exc:
                await tools_filesystem.call_mcp_tools_service(
                    core_client, "list_files", {"path": "www"}
                )

    error = error_of(exc)
    assert error["code"] == "COMPONENT_NOT_INSTALLED"
    assert released in error["message"] and ahead in error["message"]

    listing = await tools_filesystem.call_mcp_tools_service(
        core_client, "list_files", {"path": "www"}
    )
    assert isinstance(listing, dict)
