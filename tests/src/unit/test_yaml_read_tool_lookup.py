"""ha_config_get_yaml on real Home Assistant Core: one file, a glob, and the
reads that fail along the way.

The files sit in Core's config directory, and every reply comes from the
File & YAML Tools entry's own services.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import ATTR_SERVICE, EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, callback

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import reset_global_settings
from ha_mcp.tools.tools_yaml_read import register_yaml_read_tools

from ._component_filesystem import put
from ._tool_errors import error_of
from ._yaml_read_tool_core import (
    CONFIGURATION,
    FAILS_TO_READ,
    NOT_YAML,
    NOT_YAML_ERROR,
    GetYaml,
    found,
    read_failed,
    yaml_tool,
)

REST = CONFIGURATION + "rest:\n  method: GET\n"
SECRET_REST = CONFIGURATION + "rest:\n  api_key: !secret k\n"


@pytest.fixture
async def get_yaml(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> GetYaml:
    return await yaml_tool(hass, component_core_client)


async def _registered(
    client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch, **env: str
) -> bool:
    """Whether the server offers the tool with filesystem tools on, plus ``env``."""
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "true")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_global_settings()
    mcp = FastMCP("yaml-read")
    register_yaml_read_tools(mcp, client)
    return await mcp.get_tool("ha_config_get_yaml") is not None


async def test_tool_is_offered_with_yaml_editing_off(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a fragment is not an edit, so the editing flag does not hide it."""
    assert await _registered(
        core_client, monkeypatch, ENABLE_YAML_CONFIG_EDITING="false"
    )


async def test_tool_is_hidden_with_filesystem_tools_off(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It returns config-file contents, so an install that turned file reads
    off must not get them back through this tool."""
    assert not await _registered(
        core_client, monkeypatch, HAMCP_ENABLE_FILESYSTEM_TOOLS="false"
    )


async def test_named_file_returns_the_fragment_under_the_key(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="configuration.yaml")

    assert out == {
        "success": True,
        "yaml_path": "rest",
        "matches": [
            {
                "file": "configuration.yaml",
                "yaml_path": "rest",
                "content": "method: GET\n",
            }
        ],
        "count": 1,
        "files_searched": 1,
        "files_unreadable": 0,
    }


async def test_key_the_file_does_not_define_is_no_match_not_an_error(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": REST})

    out = await get_yaml(yaml_path="nope", file="configuration.yaml")

    assert out["success"] is True
    assert out["matches"] == []
    assert out["files_searched"] == 1


async def test_glob_reports_only_the_files_defining_the_key(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(
        hass,
        {
            "packages/lights.yaml": "light: []\n",
            "packages/alert2.yaml": "alert2:\n  - name: my_alert\n",
            "configuration.yaml": CONFIGURATION + "alert2: []\n",
        },
    )

    out = await get_yaml(yaml_path="alert2", file="packages/*.yaml")

    assert found(out) == ["packages/alert2.yaml"]
    assert out["files_searched"] == 2


async def test_glob_skips_folders_and_sorts_the_files(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(
        hass,
        {
            "packages/b.yaml": "k: b\n",
            "packages/nested.yaml/c.yaml": "k: c\n",
            "packages/a.yaml": "k: a\n",
        },
    )

    out = await get_yaml(yaml_path="k", file="packages/*.yaml")

    assert found(out) == ["packages/a.yaml", "packages/b.yaml"]


async def test_discovery_without_content_names_the_file_only(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"packages/alert2.yaml": "alert2:\n  - name: my_alert\n"})

    out = await get_yaml(
        yaml_path="alert2", file="packages/alert2.yaml", include_content=False
    )

    assert out["matches"] == [{"file": "packages/alert2.yaml", "yaml_path": "alert2"}]


async def test_parsed_view_keeps_a_secret_in_source_form(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": SECRET_REST})

    out = await get_yaml(
        yaml_path="rest", file="configuration.yaml", include_parsed=True
    )

    assert out["matches"][0]["parsed"] == {"api_key": "!secret k"}


async def test_parsed_view_is_left_out_unless_asked_for(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": SECRET_REST})

    out = await get_yaml(yaml_path="rest", file="configuration.yaml")

    assert "parsed" not in out["matches"][0]


async def test_missing_named_file_raises(get_yaml: GetYaml) -> None:
    with pytest.raises(ToolError, match=r"File does not exist: packages/nope\.yaml"):
        await get_yaml(yaml_path="rest", file="packages/nope.yaml")


async def test_glob_keeps_its_matches_when_one_file_is_refused(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """``packages/*`` also turns up a README the entry will not read."""
    await put(
        hass,
        {"packages/README.md": "notes\n", "packages/alert2.yaml": "alert2: []\n"},
    )

    out = await get_yaml(yaml_path="alert2", file="packages/*")

    assert found(out) == ["packages/alert2.yaml"]
    assert out["files_searched"] == 2
    (warning,) = out["warnings"]
    assert warning.startswith("packages/README.md was not searched: Path not allowed")


async def test_glob_keeps_its_matches_when_one_read_fails(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(
        hass,
        {"packages/broken.yaml": FAILS_TO_READ, "packages/alert2.yaml": "alert2: []\n"},
    )

    out = await get_yaml(yaml_path="alert2", file="packages/*.yaml")

    assert found(out) == ["packages/alert2.yaml"]
    (warning,) = out["warnings"]
    assert read_failed("packages/broken.yaml", warning)


async def test_glob_matching_no_file_is_an_empty_result(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """files_searched 0 is what tells it apart from "no file defines the key"."""
    await put(hass, {"packages/lights.yaml": "alert2: []\n"})

    out = await get_yaml(yaml_path="alert2", file="packages/alerts*.yaml")

    assert out["success"] is True
    assert out["matches"] == []
    assert out["files_searched"] == 0
    assert "warnings" not in out


async def test_one_file_glob_still_warns_when_its_read_fails(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Leniency follows from the file coming out of a glob, not from how many did."""
    await put(hass, {"packages/only.yaml": FAILS_TO_READ})

    out = await get_yaml(yaml_path="rest", file="packages/*.yaml")

    assert out["success"] is True
    assert out["files_searched"] == 1
    (warning,) = out["warnings"]
    assert read_failed("packages/only.yaml", warning)


async def test_glob_file_that_will_not_parse_is_reported_not_skipped(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """One broken package must not make the search report a clean absence."""
    await put(
        hass, {"packages/broken.yaml": NOT_YAML, "packages/good.yaml": "alert2: []\n"}
    )

    out = await get_yaml(yaml_path="alert2", file="packages/*.yaml")

    assert found(out) == ["packages/good.yaml"]
    assert out["warnings"] == [
        f"packages/broken.yaml was not searched: {NOT_YAML_ERROR}."
    ]


async def test_named_file_that_will_not_parse_raises(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """A warning beside ``count: 0`` would read as "key absent"."""
    await put(hass, {"configuration.yaml": NOT_YAML})

    with pytest.raises(ToolError) as exc:
        await get_yaml(yaml_path="rest", file="configuration.yaml")

    assert error_of(exc)["code"] == "CONFIG_INVALID"


async def test_named_file_raises_when_home_assistant_does_not_answer(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    get_yaml: GetYaml,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``success: true, count: 0`` would claim a file that was never opened."""
    await put(hass, {"configuration.yaml": REST})
    monkeypatch.setattr(
        component_core_client,
        "call_service",
        AsyncMock(side_effect=HomeAssistantConnectionError("Request timeout")),
    )

    with pytest.raises(ToolError, match="Request timeout"):
        await get_yaml(yaml_path="rest", file="configuration.yaml")


async def test_cancelled_search_is_not_reported_as_a_result(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """A search called off mid-read must not come back as one that found nothing."""
    await put(hass, {"packages/only.yaml": "rest: []\n"})
    search = asyncio.ensure_future(get_yaml(yaml_path="rest", file="packages/*.yaml"))

    @callback
    def cancel_on_read(event: Event) -> None:
        if event.data[ATTR_SERVICE] == "read_file":
            search.cancel()

    unsubscribe = hass.bus.async_listen(EVENT_CALL_SERVICE, cancel_on_read)
    try:
        await asyncio.wait([search])
    finally:
        unsubscribe()
    await hass.async_block_till_done()

    assert search.cancelled()


async def test_unreadable_files_are_counted_and_each_named(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """``count: 0`` beside ``files_searched: 3`` must not read as a search
    that looked and found nothing."""
    await put(
        hass, {f"packages/{name}.yaml": FAILS_TO_READ for name in ("a", "b", "c")}
    )

    out = await get_yaml(yaml_path="rest", file="packages/*.yaml")

    assert out["count"] == 0
    assert out["files_searched"] == 3
    assert out["files_unreadable"] == 3
    a, b, c = out["warnings"]
    assert read_failed("packages/a.yaml", a)
    assert read_failed("packages/b.yaml", b)
    assert read_failed("packages/c.yaml", c)
