"""The file tools' gates on a real ``hass``.

The beta flags decide whether the tools are registered at all. A call then
needs the File & YAML Tools entry's services, which the gate reads from Home
Assistant's live service registry and remembers for a short window. Core sets
the component's entries up, and the component's own services list, write and
delete files in the test's config directory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import DOMAIN
from ha_mcp import config as ha_config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import reset_global_settings
from ha_mcp.tools import tools_filesystem
from ha_mcp.tools.tools_filesystem import (
    FEATURE_FLAG,
    FilesystemTools,
    is_filesystem_tools_enabled,
    register_filesystem_tools,
)

from ._file_services import make_file_service
from ._tool_errors import error_of

ENTRY_NAME = "HA-MCP File & YAML Tools"
FILE_TOOLS = {"ha_list_files", "ha_read_file", "ha_write_file", "ha_delete_file"}


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """The flags are read through the cached settings."""
    reset_global_settings()
    yield
    reset_global_settings()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The clock the registry probe's memo reads; ``clock[0]`` moves it."""
    now = [1000.0]
    monkeypatch.setattr(tools_filesystem, "probe_clock", lambda: now[0])
    return now


@pytest.fixture
def auto_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Auto-backup on, which writes and deletes require, into ``tmp_path``."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    reset_global_settings()


def _put(hass: HomeAssistant, relative: str, text: str) -> Path:
    target = Path(hass.config.path(relative))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


async def _file(hass: HomeAssistant, relative: str, text: str = "a{}") -> Path:
    path: Path = await hass.async_add_executor_job(_put, hass, relative, text)
    return path


async def _list(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    listing: dict[str, Any] = await FilesystemTools(client).ha_list_files(
        path="www/", **kwargs
    )
    return listing


def _registry_reads(client: HomeAssistantClient) -> Any:
    """Spy on the service-registry read; Core still answers it."""
    return patch.object(client, "get_services", wraps=client.get_services)


async def _remove_tools_entry(hass: HomeAssistant) -> None:
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()


# --- the beta flags -------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "enabled"),
    [
        ({}, False),
        ({FEATURE_FLAG: "true", "ENABLE_BETA_FEATURES": "true"}, True),
        ({FEATURE_FLAG: "true"}, False),
    ],
    ids=["neither flag", "both flags", "beta master off"],
)
def test_the_file_tools_need_the_beta_master_flag_and_their_own(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], enabled: bool
) -> None:
    for name in (FEATURE_FLAG, "ENABLE_BETA_FEATURES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_global_settings()

    assert is_filesystem_tools_enabled() is enabled


@pytest.mark.parametrize(
    ("flag", "registered"), [("false", set()), ("true", FILE_TOOLS)], ids=["off", "on"]
)
def test_the_flag_decides_whether_the_file_tools_are_registered(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    registered: set[str],
) -> None:
    monkeypatch.setenv(FEATURE_FLAG, flag)
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    reset_global_settings()
    mcp = MagicMock()

    register_filesystem_tools(mcp, core_client)

    assert {call.args[0].__name__ for call in mcp.add_tool.call_args_list} == registered


# --- the File & YAML Tools entry -------------------------------------------------

Call = Callable[[FilesystemTools], Awaitable[Any]]


@pytest.mark.parametrize(
    "call",
    [
        lambda tools: tools.ha_list_files(path="www/"),
        lambda tools: tools.ha_read_file(path="configuration.yaml"),
    ],
    ids=["list", "read"],
)
async def test_without_the_component_a_file_tool_says_how_to_install_it(
    core_client: HomeAssistantClient, call: Call
) -> None:
    """The recipe names the default catalog and the entry to add (#1853), not
    an update: nothing is installed to update (#1996)."""
    with pytest.raises(ToolError) as exc:
        await call(FilesystemTools(core_client))

    error = error_of(exc)
    recipe = " ".join(error["suggestions"])
    assert error["code"] == "COMPONENT_NOT_INSTALLED"
    assert ENTRY_NAME in recipe
    assert 'repository_id="homeassistant-ai/ha-mcp"' in recipe
    assert "too old" not in error["message"]


async def test_with_the_server_entry_only_a_file_tool_asks_for_the_tools_entry(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The server entry answers the component's commands too (#2289), so an
    answering component does not prove the file services exist (#2292). The
    embedded server's own config directory changes nothing about that (#2329)."""
    ha_config.set_embedded_connection(
        component_core_client.base_url,
        component_core_client.token,
        config_dir=hass.config.config_dir,
    )
    try:
        with pytest.raises(ToolError) as exc:
            await _list(component_core_client)
    finally:
        ha_config.reset_embedded_connection()

    assert ENTRY_NAME in error_of(exc)["message"]


async def test_with_the_tools_entry_a_listing_keeps_only_the_pattern(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await make_file_service(hass)
    await _file(hass, "www/card.css")
    await _file(hass, "www/card.js")

    listing = await _list(core_client, pattern="*.css")

    assert [entry["name"] for entry in listing["files"]] == ["card.css"]


async def test_an_unanswered_registry_read_is_not_a_missing_component(
    core_client: HomeAssistantClient,
) -> None:
    """No answer says nothing about what is installed, so the user is not sent
    to install a component they may have."""
    with (
        patch.object(
            core_client,
            "get_services",
            side_effect=HomeAssistantConnectionError("no answer"),
        ),
        pytest.raises(ToolError) as exc,
    ):
        await _list(core_client)

    assert error_of(exc)["code"] != "COMPONENT_NOT_INSTALLED"


async def test_a_component_behind_the_server_is_refused_before_the_registry_is_read(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answering component reports its version, so the gate needs no
    registry read to refuse one older than the server."""
    await make_file_service(hass)
    released = tools_filesystem.MIN_COMPONENT_VERSION
    monkeypatch.setattr(
        tools_filesystem,
        "MIN_COMPONENT_VERSION",
        f"{int(released.split('.')[0]) + 1}.0.0",
    )

    with (
        _registry_reads(component_core_client) as registry,
        pytest.raises(ToolError) as exc,
    ):
        await _list(component_core_client)

    assert "too old" in error_of(exc)["message"]
    registry.assert_not_awaited()


# --- the registry probe's memo ---------------------------------------------------


async def test_the_registry_is_read_once_per_window(
    hass: HomeAssistant, core_client: HomeAssistantClient, clock: list[float]
) -> None:
    """The read fetches Home Assistant's whole service registry, and every
    gated call would otherwise pay for it."""
    await make_file_service(hass)
    await _file(hass, "www/card.css")

    with _registry_reads(core_client) as registry:
        await _list(core_client)
        first = registry.await_count
        clock[0] += tools_filesystem.LIVE_TOOLS_PROBE_TTL_S / 2
        await _list(core_client)
        within = registry.await_count
        clock[0] += tools_filesystem.LIVE_TOOLS_PROBE_TTL_S
        await _list(core_client)

    assert within == first
    assert registry.await_count == within + 1


async def test_an_entry_added_mid_session_works_once_the_window_passes(
    hass: HomeAssistant, core_client: HomeAssistantClient, clock: list[float]
) -> None:
    """The verdict is remembered when it is "no services" too, or an install
    without the entry would read the whole registry on every call. The price
    is one window before the entry the user adds is seen, with no server
    restart (#2291, #2302)."""
    with pytest.raises(ToolError):
        await _list(core_client)
    await make_file_service(hass)
    await _file(hass, "www/card.css")

    clock[0] += tools_filesystem.LIVE_TOOLS_PROBE_TTL_S / 2
    with _registry_reads(core_client) as registry, pytest.raises(ToolError) as exc:
        await _list(core_client)
    clock[0] += tools_filesystem.LIVE_TOOLS_PROBE_TTL_S
    listing = await _list(core_client)

    assert ENTRY_NAME in error_of(exc)["message"]
    registry.assert_not_awaited()
    assert listing["success"] is True


async def test_an_entry_removed_mid_session_is_reported_once_the_window_passes(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    clock: list[float],
) -> None:
    """A remembered "services exist" must not send calls to services that are
    gone, which fail without saying what to do (#2302)."""
    await make_file_service(hass)
    await _file(hass, "www/card.css")
    await _list(component_core_client)

    await _remove_tools_entry(hass)
    clock[0] += tools_filesystem.LIVE_TOOLS_PROBE_TTL_S + 1
    with pytest.raises(ToolError) as exc:
        await _list(component_core_client)

    assert ENTRY_NAME in error_of(exc)["message"]


async def test_concurrent_calls_read_the_registry_once(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    second_client: HomeAssistantClient,
    clock: list[float],
) -> None:
    """Two calls that find no remembered verdict wait for one read, on a first
    call and again when the window has passed. One client makes its calls one
    after the other, the other two at a time."""
    await make_file_service(hass)
    await _file(hass, "www/card.css")

    with _registry_reads(core_client) as alone, _registry_reads(second_client) as pair:
        await _list(core_client)
        await asyncio.gather(_list(second_client), _list(second_client))
        first = (alone.await_count, pair.await_count)
        clock[0] += tools_filesystem.LIVE_TOOLS_PROBE_TTL_S + 1
        await _list(core_client)
        await asyncio.gather(_list(second_client), _list(second_client))

    assert first[1] == first[0]
    assert pair.await_count - first[1] == alone.await_count - first[0]


# --- what the write tools do -----------------------------------------------------


@pytest.mark.usefixtures("auto_backup")
async def test_a_write_replaces_the_file_when_told_to_overwrite(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await make_file_service(hass)
    target = await _file(hass, "www/test.css", "old")

    result = await FilesystemTools(core_client).ha_write_file(
        path="www/test.css",
        content=".test { color: red; }",
        overwrite=True,
        create_dirs=False,
    )

    assert result["success"] is True
    assert (
        await hass.async_add_executor_job(target.read_text) == ".test { color: red; }"
    )


@pytest.mark.usefixtures("auto_backup")
@pytest.mark.parametrize(("confirm", "kept"), [(False, True), (True, False)])
async def test_a_file_is_deleted_only_when_confirmed(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    confirm: bool,
    kept: bool,
) -> None:
    """An unconfirmed delete changes nothing, so it takes no backup either."""
    await make_file_service(hass)
    target = await _file(hass, "www/test.css")
    tools = FilesystemTools(core_client)

    if confirm:
        assert (await tools.ha_delete_file(path="www/test.css", confirm=True))[
            "success"
        ] is True
    else:
        with pytest.raises(ToolError) as exc:
            await tools.ha_delete_file(path="www/test.css", confirm=False)
        assert "not confirmed" in error_of(exc)["message"].lower()

    assert await hass.async_add_executor_job(target.exists) is kept
    snapshots = list((tmp_path / "backups").glob("file.*.yaml"))
    assert len(snapshots) == int(confirm)
