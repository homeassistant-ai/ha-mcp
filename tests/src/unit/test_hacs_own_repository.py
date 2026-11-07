"""ha-mcp's own integration stays on the one store repository that owns it.

Regression for #2993: the install guidance names the main repository, and on
HACS a download of it on top of a mirror install left two records claiming
``custom_components/ha_mcp_tools``; the Marketplace then refused to update
either. A download of either of ha-mcp's repositories now goes to the one
that is installed, and two installed records are refused with the way out.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.hacs_backend import HACS_API
from ha_mcp.tools.hacs_own_repository import route_own_repository_download
from ha_mcp.tools.tools_hacs import HacsTools

MAIN = {"id": "1056618941", "full_name": "homeassistant-ai/ha-mcp", "name": "HA-MCP"}
MIRROR = {
    "id": "1289599380",
    "full_name": "homeassistant-ai/ha-mcp-integration",
    "name": "HA-MCP",
}
OTHER = {"id": "441028036", "full_name": "piitaya/lovelace-mushroom", "name": "M"}


def _ws(repos):
    ws = AsyncMock()

    async def send_command(command, **kwargs):
        if command.endswith("/list"):
            return {"success": True, "result": repos}
        return {"success": True, "result": {"status": "ok"}}

    ws.send_command = AsyncMock(side_effect=send_command)
    return ws


def _installed(repo, version="v9.1.0"):
    return {**repo, "installed": True, "installed_version": version}


async def test_other_repositories_pass_through_without_a_store_call():
    ws = _ws([_installed(MIRROR)])
    assert await route_own_repository_download(ws, HACS_API, "piitaya/x", None) == (
        "piitaya/x",
        None,
        None,
    )
    ws.send_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("requested", "installed", "owner"),
    [
        ("homeassistant-ai/ha-mcp", MIRROR, "homeassistant-ai/ha-mcp-integration"),
        ("homeassistant-ai/ha-mcp-integration", MAIN, "homeassistant-ai/ha-mcp"),
        ("1056618941", MIRROR, "homeassistant-ai/ha-mcp-integration"),
    ],
)
async def test_the_installed_repository_wins(requested, installed, owner):
    ws = _ws([MAIN, MIRROR, OTHER, _installed(installed)])
    target, version, note = await route_own_repository_download(
        ws, HACS_API, requested, "v9.1.1"
    )
    assert (target, version) == (owner, "v9.1.1")
    assert note is not None
    assert owner in note


async def test_a_request_for_the_installed_repository_is_untouched():
    ws = _ws([_installed(MAIN), MIRROR])
    assert await route_own_repository_download(
        ws, HACS_API, "homeassistant-ai/ha-mcp", None
    ) == ("homeassistant-ai/ha-mcp", None, None)


async def test_nothing_installed_keeps_the_requested_repository():
    ws = _ws([MAIN, MIRROR])
    assert await route_own_repository_download(
        ws, HACS_API, "homeassistant-ai/ha-mcp", None
    ) == ("homeassistant-ai/ha-mcp", None, None)


async def test_two_installed_records_are_refused_with_the_way_out():
    ws = _ws([_installed(MAIN, "v9.1.1"), _installed(MIRROR)])
    with pytest.raises(ToolError) as excinfo:
        await route_own_repository_download(
            ws, HACS_API, "homeassistant-ai/ha-mcp", None
        )
    message = str(excinfo.value)
    assert "homeassistant-ai/ha-mcp" in message
    assert "homeassistant-ai/ha-mcp-integration" in message
    assert "Devices & services" in message


async def test_download_of_the_main_repository_installs_from_the_mirror():
    """The server's own recipe no longer creates the second record."""
    ws = _ws([MAIN, _installed(MIRROR)])

    async def identity(_client, data):
        return data

    with (
        patch(
            "ha_mcp.tools.tools_hacs.detect_store_api",
            new=AsyncMock(return_value=HACS_API),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch("ha_mcp.tools.tools_hacs.add_timezone_metadata", new=identity),
        patch(
            "ha_mcp.tools.tools_hacs.wait_for_repo_registration",
            new=AsyncMock(return_value=_installed(MIRROR)),
        ) as resolve,
    ):
        result = await HacsTools(MagicMock()).ha_manage_hacs(
            action="download", repository_id="homeassistant-ai/ha-mcp"
        )

    assert resolve.await_args.args[1] == "homeassistant-ai/ha-mcp-integration"
    install = [
        call
        for call in ws.send_command.await_args_list
        if call.args[0] == "hacs/repository/download"
    ]
    assert len(install) == 1
    assert install[0].kwargs["repository"] == "1289599380"
    assert result["success"] is True
    assert any("rerouted" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("requested", "installed", "version", "expected"),
    [
        (MAIN, MIRROR, "v9.1.2.dev3397", "v9.1.2-dev.3397"),
        (MIRROR, MAIN, "v9.1.2-dev.3397", "v9.1.2.dev3397"),
        (MAIN, MIRROR, "v9.1.1", "v9.1.1"),
    ],
)
async def test_a_development_ref_is_renamed_for_the_target(
    requested, installed, version, expected
):
    """The repositories tag dev builds differently, so a rerouted dev request
    names the build the way the installed repository tags it."""
    ws = _ws([requested, _installed(installed)])
    target, target_version, note = await route_own_repository_download(
        ws, HACS_API, requested["full_name"], version
    )
    assert target == installed["full_name"]
    assert target_version == expected
    assert (expected in note) == (expected != version)


async def test_a_numeric_id_of_another_repository_passes_through():
    """Another repository's id costs no store call: its download stays one
    store command, as the Marketplace tests sequence it."""
    ws = _ws([OTHER, _installed(MIRROR)])
    assert await route_own_repository_download(ws, HACS_API, "441028036", None) == (
        "441028036",
        None,
        None,
    )
    ws.send_command.assert_not_awaited()


async def test_a_message_merely_containing_the_words_is_not_an_ownership_refusal():
    from ha_mcp.client.rest_client import HomeAssistantCommandError
    from ha_mcp.tools.hacs_backend import raise_store_error

    err = HomeAssistantCommandError(
        "Could not write directory belongs to nobody.txt: permission denied"
    )
    with pytest.raises(ToolError) as excinfo:
        raise_store_error(err, {"command": "marketplace/repository/install"})
    assert "already provides" not in str(excinfo.value)


async def test_the_store_refusal_names_the_owner_to_update():
    """A download the store refuses because another installed record owns
    the integration's directory points at that owner, not at a generic
    service failure."""
    from ha_mcp.client.rest_client import HomeAssistantCommandError
    from ha_mcp.tools.hacs_backend import raise_store_error

    err = HomeAssistantCommandError(
        "The ha_mcp_tools directory belongs to homeassistant-ai/ha-mcp-integration"
    )
    with pytest.raises(ToolError) as excinfo:
        raise_store_error(err, {"command": "marketplace/repository/install"})
    suggestions = " ".join(json.loads(str(excinfo.value))["error"]["suggestions"])
    assert (
        'ha_manage_hacs(action="download", '
        'repository_id="homeassistant-ai/ha-mcp-integration")' in suggestions
    )


async def test_a_failed_list_lookup_leaves_the_request_alone():
    """The store's list failing is the download's problem to report, with the
    download's own context; the lookup never speaks for it."""
    from ha_mcp.client.rest_client import HomeAssistantCommandError

    ws = AsyncMock()
    ws.send_command = AsyncMock(side_effect=HomeAssistantCommandError("kaboom"))
    assert await route_own_repository_download(
        ws, HACS_API, "homeassistant-ai/ha-mcp", None
    ) == ("homeassistant-ai/ha-mcp", None, None)


async def test_a_release_the_target_tags_differently_asks_for_its_own_tag():
    """Before 9.0 the mirror tagged releases with the component's numbers, so
    a request for such a version cannot be renamed; it is refused with the
    way to list the installed repository's tags instead of a store 404."""
    ws = _ws([MAIN, _installed(MIRROR)])
    with pytest.raises(ToolError) as excinfo:
        await route_own_repository_download(
            ws, HACS_API, "homeassistant-ai/ha-mcp", "v8.6.0"
        )
    error = json.loads(str(excinfo.value))["error"]
    assert "v8.6.0" in error["message"]
    assert any("include_releases=True" in s for s in error["suggestions"])


async def test_the_punctuated_refusal_keeps_the_owner_clean():
    from ha_mcp.client.rest_client import HomeAssistantCommandError
    from ha_mcp.tools.hacs_backend import raise_store_error

    err = HomeAssistantCommandError(
        "Command failed: The ha_mcp_tools directory belongs to homeassistant-ai/ha-mcp."
    )
    with pytest.raises(ToolError) as excinfo:
        raise_store_error(err, {"command": "marketplace/repository/install"})
    suggestions = " ".join(json.loads(str(excinfo.value))["error"]["suggestions"])
    assert 'repository_id="homeassistant-ai/ha-mcp")' in suggestions
    assert 'repository_id="homeassistant-ai/ha-mcp.")' not in suggestions
