"""Blueprints: the pre-write snapshot of ``ha_manage_blueprints`` (#2329).

Blueprints are real files under Core's config directory, loaded by Core's
blueprint integration; the File & YAML Tools entry reads the installed text and
Core's ``blueprint/*`` commands write it back. The tool's own decorator is what
captures, so those tests call the tool.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantError
from ha_mcp.tools.blueprint_sources import resolve_blueprint_source
from ha_mcp.tools.tools_blueprints import BlueprintTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import handler_for as _handler
from ._backup_core import snapshot_manager as snapshot_manager
from ._command_faults import received
from ._config_dir_yaml import write_config_files

PATH = "user/motion.yaml"
SOURCE_URL = "https://example.com/motion.yaml"
BLUEPRINT = f"""\
blueprint:
  name: Motion Light
  domain: automation
  source_url: {SOURCE_URL}
triggers: []
actions: []
"""
SCRIPT_BLUEPRINT = """\
blueprint:
  name: Motion Script
  domain: script
sequence: []
"""


async def _install(hass: HomeAssistant, domain: str, text: str) -> None:
    await write_config_files(hass, {f"blueprints/{domain}/{PATH}": text})


def _on_disk(hass: HomeAssistant, domain: str) -> str | None:
    try:
        with open(
            hass.config.path(f"blueprints/{domain}/{PATH}"), encoding="utf-8"
        ) as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def _installed_name(hass: HomeAssistant, domain: str) -> str | None:
    """The name of the blueprint installed at ``PATH``; Core adds an empty
    ``input`` when it writes one, so the text is not compared."""
    text = _on_disk(hass, domain)
    return None if text is None else str(yaml.safe_load(text)["blueprint"]["name"])


@pytest.fixture(autouse=True)
async def blueprint_domains(
    hass: HomeAssistant, hass_config_dir: str, snapshot_manager: bm.BackupManager
) -> None:
    """Core's blueprint integration, which the automation and script
    integrations load; Core reads ``configuration.yaml`` when they reload."""
    await write_config_files(hass, {"configuration.yaml": ""})
    for domain in ("automation", "script"):
        assert await async_setup_component(hass, domain, {})


async def test_both_blueprint_domains_are_registered_and_snapshot_as_text(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await _install(hass, "automation", BLUEPRINT)
    await _install(hass, "script", SCRIPT_BLUEPRINT)

    for domain, text in (
        ("blueprint_automation", BLUEPRINT),
        ("blueprint_script", SCRIPT_BLUEPRINT),
    ):
        assert domain in file_snapshot_manager.supported_domains()
        captured = await file_snapshot_manager.maybe_snapshot(domain, PATH, force=True)
        assert captured is not None
        held = file_snapshot_manager.read_snapshot(captured.name)
        assert held["kind"] == "text"
        assert held["config"] == text


async def test_the_installed_file_is_read_without_contacting_its_source(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """The installed-file read is the faithful copy: no listing, no re-fetch."""
    await _install(hass, "automation", BLUEPRINT)

    with received(hass, "blueprint/list", "blueprint/import") as seen:
        held = await _handler(file_snapshot_manager, "blueprint_automation").fetch(
            core_client, PATH
        )

    assert held == BLUEPRINT
    assert seen == {"blueprint/list": [], "blueprint/import": []}


async def test_each_domain_reads_its_own_blueprint_directory(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await _install(hass, "automation", BLUEPRINT)
    await _install(hass, "script", SCRIPT_BLUEPRINT)

    by_domain = {
        domain: await _handler(file_snapshot_manager, f"blueprint_{domain}").fetch(
            core_client, PATH
        )
        for domain in ("automation", "script")
    }

    assert by_domain == {"automation": BLUEPRINT, "script": SCRIPT_BLUEPRINT}


async def test_a_blueprint_that_is_not_installed_has_nothing_to_back_up(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    assert (
        await _handler(file_snapshot_manager, "blueprint_automation").fetch(
            core_client, PATH
        )
        is None
    )


async def test_the_source_url_is_never_a_snapshot_source(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """``get`` may fall back to re-downloading ``source_url``; a snapshot must
    not, because a restore from it could write what the author publishes now
    rather than what the delete destroyed. With no installed-file tier (no tools
    entry here) the capture skips outright, and Core is never asked to import."""
    await _install(hass, "automation", BLUEPRINT)
    aioclient_mock.get(SOURCE_URL, text=BLUEPRINT)

    with received(hass, "blueprint/import") as seen:
        assert (
            await _handler(snapshot_manager, "blueprint_automation").fetch(
                core_client, PATH
            )
            is None
        )
        # The same ladder with the URL allowed is what ``get`` walks; it does ask.
        await resolve_blueprint_source(
            core_client, "automation", PATH, source_url=SOURCE_URL
        )

    assert len(seen["blueprint/import"]) == 1
    assert aioclient_mock.call_count == 1


async def test_restoring_a_blueprint_writes_the_snapshot_over_the_installed_file(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await _install(
        hass,
        "automation",
        BLUEPRINT.replace("Motion Light", "Old"),
    )

    await _handler(file_snapshot_manager, "blueprint_automation").restore(
        core_client, PATH, BLUEPRINT
    )

    assert _installed_name(hass, "automation") == "Motion Light"


async def test_a_blueprint_core_refuses_is_raised(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    with pytest.raises(HomeAssistantError):
        await _handler(file_snapshot_manager, "blueprint_automation").restore(
            core_client, PATH, "not: a blueprint\n"
        )


async def test_a_captured_blueprint_comes_back_through_the_manager(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    """The registered handler pair is what the decorator reaches, so drive it
    through a capture and a restore."""
    await _install(hass, "script", SCRIPT_BLUEPRINT)
    captured = await file_snapshot_manager.maybe_snapshot(
        "blueprint_script", PATH, force=True
    )
    assert captured is not None
    await _install(hass, "script", SCRIPT_BLUEPRINT.replace("Motion Script", "Edited"))

    result = await file_snapshot_manager.restore_snapshot(captured.name)

    assert _installed_name(hass, "script") == "Motion Script"
    assert result["domain"] == "blueprint_script"


# ---------------------------------------------------------------- the tool's decorator


@pytest.fixture
def tool(core_client: HomeAssistantClient) -> BlueprintTools:
    return BlueprintTools(core_client)


async def _snapshots(manager: bm.BackupManager) -> list[dict[str, Any]]:
    entries, _ = await manager.list_edits_and_legacy(domain=None)
    return [e for e in entries if e["domain"].startswith("blueprint_")]


async def test_listing_and_reading_a_blueprint_capture_nothing(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager, tool: BlueprintTools
) -> None:
    await _install(hass, "automation", BLUEPRINT)

    await tool.ha_manage_blueprints(action="list", domain="automation")
    await tool.ha_manage_blueprints(action="get", domain="automation", path=PATH)

    assert await _snapshots(file_snapshot_manager) == []


async def test_an_unconfirmed_delete_captures_nothing_and_deletes_nothing(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager, tool: BlueprintTools
) -> None:
    """The tool refuses an unconfirmed delete and changes nothing, so capturing
    for it would pay a component read and, with no component, an outbound
    re-fetch of a third-party source_url for no reason."""
    await _install(hass, "automation", BLUEPRINT)

    with received(hass, "blueprint/import") as seen, pytest.raises(ToolError):
        await tool.ha_manage_blueprints(
            action="delete", domain="automation", path=PATH, confirm=False
        )

    assert await _snapshots(file_snapshot_manager) == []
    assert seen["blueprint/import"] == []
    assert _on_disk(hass, "automation") == BLUEPRINT


@pytest.mark.parametrize("domain", ["automation", "script"])
async def test_a_confirmed_delete_captures_the_installed_text_under_its_domain(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    tool: BlueprintTools,
    domain: str,
) -> None:
    text = BLUEPRINT if domain == "automation" else SCRIPT_BLUEPRINT
    await _install(hass, domain, text)

    await tool.ha_manage_blueprints(
        action="delete", domain=domain, path=PATH, confirm=True
    )

    (row,) = await _snapshots(file_snapshot_manager)
    assert row["domain"] == f"blueprint_{domain}"
    assert row["entity_id"] == PATH
    held = file_snapshot_manager.read_snapshot(row["name"])
    assert held["config"] == text
    assert _on_disk(hass, domain) is None


async def test_an_overwriting_save_captures_the_file_it_replaces(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager, tool: BlueprintTools
) -> None:
    """The previous file is destroyed by the save, so the snapshot is the only way
    back; a save of ``user/motion`` without the extension replaces
    ``user/motion.yaml``, so the snapshot keys on the normalised path."""
    await _install(hass, "automation", BLUEPRINT)
    edited = BLUEPRINT.replace("Motion Light", "Edited")

    await tool.ha_manage_blueprints(
        action="save",
        domain="automation",
        path="user/motion",
        yaml=edited,
        overwrite=True,
    )

    (row,) = await _snapshots(file_snapshot_manager)
    assert row["entity_id"] == PATH
    assert file_snapshot_manager.read_snapshot(row["name"])["config"] == BLUEPRINT
    assert _installed_name(hass, "automation") == "Edited"


async def test_a_save_to_a_free_path_captures_nothing(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager, tool: BlueprintTools
) -> None:
    """Without ``overwrite`` a save either lands on a free path (nothing to
    snapshot) or is refused for already existing; neither destroys anything."""
    await tool.ha_manage_blueprints(
        action="save", domain="automation", path=PATH, yaml=BLUEPRINT
    )

    assert await _snapshots(file_snapshot_manager) == []
    assert _on_disk(hass, "automation") is not None


async def test_an_import_captures_nothing(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    tool: BlueprintTools,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Import addresses a path core chooses, not the ``path`` argument, so the
    decorator's id would name the wrong file."""
    aioclient_mock.get(SOURCE_URL, text=BLUEPRINT)

    await tool.ha_manage_blueprints(action="import", url=SOURCE_URL)

    assert await _snapshots(file_snapshot_manager) == []
