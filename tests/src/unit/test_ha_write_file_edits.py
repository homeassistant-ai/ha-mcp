"""ha_write_file(edits=...) routes to the component's edit_file service.

Every call runs on a real ``hass`` with the File & YAML Tools entry set up, so
the component's own services read, edit and write files in the test's config
directory, and the mandatory pre-write snapshot is real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import file_ops
from custom_components.ha_mcp_tools.const import DOMAIN
from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools import tools_filesystem, tools_yaml_config
from ha_mcp.tools.resource_patch import MAX_EDITS
from ha_mcp.tools.tools_filesystem import (
    EDIT_FILE_SERVICE,
    FilesystemTools,
    call_mcp_tools_service,
)
from ha_mcp.tools.tools_yaml_config import YamlConfigTools
from ha_mcp.utils.data_paths import get_data_dir
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._edit_yaml import make_edit_yaml

EDITS = [{"old": "30000", "new": "10000"}]
CARD = "const REFRESH = 30000;\n"
WRITE_SERVICES = {EDIT_FILE_SERVICE, "write_file", "edit_yaml_config"}


@pytest.fixture
def auto_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Auto-backup on (file writes require it), into ``tmp_path``."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "0")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ENABLE_YAML_EDIT_CONFIRM", "false")
    reset_global_settings()
    yield
    reset_global_settings()


@pytest.fixture
async def tools_client(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    auto_backup: None,
) -> HomeAssistantClient:
    """The server's client on a ``hass`` with the File & YAML Tools entry."""
    await make_edit_yaml(hass)
    return component_core_client


def _manager(client: HomeAssistantClient) -> bm.BackupManager:
    return bm.get_backup_manager(client, get_global_settings())


async def _write(hass: HomeAssistant, path: str, text: str) -> Path:
    target = Path(hass.config.path(path))

    def write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    await hass.async_add_executor_job(write)
    return target


async def _read(hass: HomeAssistant, path: str) -> str:
    return await hass.async_add_executor_job(
        Path(hass.config.path(path)).read_text, "utf-8"
    )


def _service_spy() -> Any:
    """Spy on the component service calls the file tools make; they still run."""
    return patch.object(
        tools_filesystem, "call_mcp_tools_service", wraps=call_mcp_tools_service
    )


def _services(spy: MagicMock) -> list[str]:
    return [call.args[1] for call in spy.await_args_list]


async def test_edits_reach_the_edit_service_without_a_whole_file_write(
    hass: HomeAssistant, tools_client: HomeAssistantClient
) -> None:
    await _write(hass, "www/card.js", CARD)

    with _service_spy() as service:
        result = await FilesystemTools(tools_client).ha_write_file(
            path="www/card.js", edits=EDITS
        )

    assert result["success"] is True
    assert await _read(hass, "www/card.js") == "const REFRESH = 10000;\n"
    assert [s for s in _services(service) if s in WRITE_SERVICES] == [EDIT_FILE_SERVICE]


async def test_content_write_works_on_a_component_without_edit_file(
    hass: HomeAssistant, tools_client: HomeAssistantClient
) -> None:
    """Whole-file writes must not force a component update."""
    hass.services.async_remove(DOMAIN, EDIT_FILE_SERVICE)

    result = await FilesystemTools(tools_client).ha_write_file(
        path="www/a.css", content="x"
    )

    assert result["success"] is True
    assert await _read(hass, "www/a.css") == "x"


@pytest.mark.parametrize(
    "modes",
    [{}, {"content": "x", "edits": EDITS}],
    ids=["neither", "both"],
)
async def test_invalid_write_mode_is_refused_before_backup(
    hass: HomeAssistant,
    tools_client: HomeAssistantClient,
    modes: dict[str, Any],
) -> None:
    """A call that cannot write must not take a snapshot, which could rotate out
    an older restore point."""
    await _write(hass, "www/a.css", CARD)

    with pytest.raises(ToolError) as excinfo:
        await FilesystemTools(tools_client).ha_write_file(path="www/a.css", **modes)

    assert json.loads(str(excinfo.value))["error"]["code"] == (
        "VALIDATION_INVALID_PARAMETER"
    )
    assert _manager(tools_client).list_snapshots() == []
    assert await _read(hass, "www/a.css") == CARD


@pytest.mark.parametrize(
    "modes",
    [{}, {"content": "x", "edits": EDITS}],
    ids=["neither", "both"],
)
async def test_invalid_write_mode_is_not_hidden_behind_a_missing_component(
    core_client: HomeAssistantClient, auto_backup: None, modes: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await FilesystemTools(core_client).ha_write_file(path="www/a.css", **modes)

    assert json.loads(str(excinfo.value))["error"]["code"] == (
        "VALIDATION_INVALID_PARAMETER"
    )


async def test_component_without_edit_file_gets_an_update_prompt(
    hass: HomeAssistant, tools_client: HomeAssistantClient
) -> None:
    """Asks the registry, not a version: an unreleased build reports the
    previous release's number with the service, and a dev pre-release the next
    release's number, also without it."""
    await _write(hass, "www/a.css", CARD)
    hass.services.async_remove(DOMAIN, EDIT_FILE_SERVICE)

    with _service_spy() as service, pytest.raises(ToolError) as excinfo:
        await FilesystemTools(tools_client).ha_write_file(path="www/a.css", edits=EDITS)

    assert json.loads(str(excinfo.value))["error"]["code"] == (
        "COMPONENT_NOT_INSTALLED"
    )
    assert not WRITE_SERVICES & set(_services(service))
    assert await _read(hass, "www/a.css") == CARD


async def test_component_refusal_reaches_the_model_with_its_reason(
    hass: HomeAssistant, tools_client: HomeAssistantClient
) -> None:
    await _write(hass, "www/a.css", "body {}\n")

    with pytest.raises(ToolError) as excinfo:
        await FilesystemTools(tools_client).ha_write_file(path="www/a.css", edits=EDITS)

    payload = json.loads(str(excinfo.value))
    # Same shape as ha_config_set_dashboard_resource(edits=...) refusals.
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "www/a.css" in payload["error"]["message"]
    assert payload["reason"] == "old_not_found"
    assert payload["edit_index"] == 0
    assert payload["path"] == "www/a.css"
    assert await _read(hass, "www/a.css") == "body {}\n"


async def test_a_file_refusal_of_an_edit_is_a_service_failure(
    tools_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await FilesystemTools(tools_client).ha_write_file(
            path="www/missing.css", edits=EDITS
        )

    payload = json.loads(str(excinfo.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert payload["reason"] == "not_found"


def test_server_and_component_allow_the_same_number_of_edits() -> None:
    """Drift would surface as the component schema's opaque length error."""
    assert file_ops.MAX_EDITS == MAX_EDITS


async def test_edits_are_snapshotted_before_the_file_changes(
    hass: HomeAssistant, tools_client: HomeAssistantClient
) -> None:
    """An edit overwrites the file, so it must take the mandatory backup even
    though it sends neither content nor overwrite."""
    await _write(hass, "www/card.js", CARD)

    await FilesystemTools(tools_client).ha_write_file(path="www/card.js", edits=EDITS)

    manager = _manager(tools_client)
    (snapshot,) = manager.list_snapshots(domain="file")
    assert manager.read_snapshot(snapshot["name"])["config"] == CARD


@pytest.mark.parametrize("second", ["file", "yaml"])
async def test_parallel_writes_snapshot_and_write_one_at_a_time(
    hass: HomeAssistant, tools_client: HomeAssistantClient, second: str
) -> None:
    """A second snapshot taken before the first write finishes holds the state
    before both, so restoring it would also remove the first edit. The two paths
    could be a link and its target, so the lock can't be keyed by path; and
    ha_config_set_yaml writes themes/ too, so it takes the same lock."""
    await _write(hass, "themes/dark.yaml", "dark:\n  primary-color: '30000'\n")
    await _write(hass, "www/link_to_dark.yaml", "primary: 30000\n")
    manager = _manager(tools_client)
    files = FilesystemTools(tools_client)
    if second == "file":
        other = files.ha_write_file(path="www/link_to_dark.yaml", edits=EDITS)
    else:
        other = YamlConfigTools(tools_client).ha_config_set_yaml(
            yaml_path="dark",
            action="replace",
            content="primary-color: red\n",
            file="themes/dark.yaml",
        )
    events: list[str] = []
    take_snapshot = manager.maybe_snapshot
    second_snapshot = asyncio.Event()

    async def snapshot(*args: Any, **kwargs: Any) -> Any:
        events.append("snapshot")
        if events.count("snapshot") == 2:
            second_snapshot.set()
        return await take_snapshot(*args, **kwargs)

    async def service(*args: Any, **kwargs: Any) -> Any:
        written = args[1] in WRITE_SERVICES
        if written:
            events.append("write")
            if events.count("write") == 1:
                # Hold the first write open so the other call gets every chance
                # to take its snapshot now; with the lock it waits instead.
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(0.2):
                        await second_snapshot.wait()
        result = await call_mcp_tools_service(*args, **kwargs)
        if written:
            events.append("written")
        return result

    with (
        patch.object(manager, "maybe_snapshot", snapshot),
        patch.object(tools_filesystem, "call_mcp_tools_service", service),
        patch.object(tools_yaml_config, "call_mcp_tools_service", service),
    ):
        await asyncio.gather(
            files.ha_write_file(path="themes/dark.yaml", edits=EDITS), other
        )

    # Each write has finished before the next snapshot is taken.
    assert events == ["snapshot", "write", "written"] * 2


async def test_edits_cannot_probe_a_file_that_defines_a_hidden_item(
    hass: HomeAssistant, tools_client: HomeAssistantClient
) -> None:
    """Which edit failed would tell the caller whether a guessed text is in a
    file ha_read_file refuses to show."""
    # The hidden set is read from Core's entity registry.
    assert await async_setup_component(hass, "config", {})
    package = "script:\n  secret_door:\n    sequence: []\n# 30000\n"
    await _write(hass, "www/a.yaml", package)
    save_visibility_config(
        get_data_dir(),
        VisibilityConfig(
            enabled=True, enforce=True, deny_entity_ids=["script.secret_door"]
        ),
    )

    with _service_spy() as service, pytest.raises(ToolError) as excinfo:
        await FilesystemTools(tools_client).ha_write_file(
            path="www/a.yaml", edits=EDITS
        )

    error = json.loads(str(excinfo.value))["error"]
    assert error["code"] == "ENTITY_VISIBILITY_ENFORCED"
    assert not WRITE_SERVICES & set(_services(service))
    assert await _read(hass, "www/a.yaml") == package
    # A file that defines no hidden item is edited under the same settings.
    await _write(
        hass, "www/b.yaml", "script:\n  front_door:\n    sequence: []\n# 30000\n"
    )
    await FilesystemTools(tools_client).ha_write_file(path="www/b.yaml", edits=EDITS)
    assert "10000" in await _read(hass, "www/b.yaml")
