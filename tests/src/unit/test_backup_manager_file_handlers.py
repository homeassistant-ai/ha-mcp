"""The file and YAML handlers: reading what is on disk, writing it back.

Core's config directory is this test's ``tmp_path``, and Core sets the File &
YAML Tools entry up, which registers the services the handlers call and the
``ha_mcp_tools/*`` commands the server reads the component's capabilities from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.ha_mcp_tools.const import DOMAIN
from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantError

from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import handler_for as _handler
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files


def _read(hass: HomeAssistant, path: str) -> str:
    return Path(hass.config.path(path)).read_text(encoding="utf-8")


async def test_a_files_content_is_read_back(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await write_config_files(hass, {"www/x.css": "body"})

    assert (
        await _handler(file_snapshot_manager, "file").fetch(core_client, "www/x.css")
        == "body"
    )


async def test_a_file_that_does_not_exist_has_nothing_to_back_up(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    """A brand-new write has no prior content, so capture skips."""
    assert (
        await _handler(file_snapshot_manager, "file").fetch(core_client, "www/x.css")
        is None
    )


async def test_a_binary_file_is_skipped_not_failed(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """Snapshots store text only; a binary file would otherwise block its write."""
    await write_config_files(hass, {"www/logo.png": ""})
    await hass.async_add_executor_job(
        Path(hass.config.path("www/logo.png")).write_bytes,
        b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe",
    )

    assert (
        await _handler(file_snapshot_manager, "file").fetch(core_client, "www/logo.png")
        is None
    )


async def test_a_read_failure_other_than_a_missing_file_is_raised(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A path outside what the tools may read must not be treated as "nothing to
    back up": the write it guards would be let through un-backed-up."""
    await write_config_files(hass, {".storage/core.config": "{}"})
    with pytest.raises(ToolError, match="not allowed"):
        await _handler(file_snapshot_manager, "file").fetch(
            core_client, ".storage/core.config"
        )


async def test_restoring_a_file_overwrites_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await write_config_files(hass, {"www/x.css": "old"})

    await _handler(file_snapshot_manager, "file").restore(
        core_client, "www/x.css", "new body"
    )

    assert _read(hass, "www/x.css") == "new body"


async def test_a_failed_file_write_is_raised(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    with pytest.raises(HomeAssistantError):
        await _handler(file_snapshot_manager, "file").restore(
            core_client, "forbidden.txt", "body"
        )


_CONFIGURATION = "rest:\n  - resource: http://a\n    scan_interval: 60\n"


async def test_a_yaml_key_is_read_as_the_text_of_its_subtree(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """The component (which keeps comments and ``!secret`` tags) extracts the
    subtree; the server forwards it."""
    await write_config_files(
        hass, {"configuration.yaml": _CONFIGURATION + "other: 1\n"}
    )

    held = await _handler(file_snapshot_manager, "yaml").fetch(
        core_client, "configuration.yaml::rest"
    )

    assert "resource: http://a" in held
    assert "other" not in held


async def test_an_absent_yaml_key_has_nothing_to_back_up(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await write_config_files(hass, {"configuration.yaml": _CONFIGURATION})

    assert (
        await _handler(file_snapshot_manager, "yaml").fetch(
            core_client, "configuration.yaml::nope"
        )
        is None
    )


async def test_a_yaml_file_that_does_not_exist_has_nothing_to_back_up(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    assert (
        await _handler(file_snapshot_manager, "yaml").fetch(
            core_client, "packages/missing.yaml::rest"
        )
        is None
    )


async def test_a_yaml_read_failure_other_than_a_missing_file_is_raised(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """The mandatory gate let this write through on the promise that it is
    backed up, so a failed read must not silently produce no backup."""
    await write_config_files(hass, {".storage/core.yaml": "rest: []\n"})
    with pytest.raises(HomeAssistantError):
        await _handler(file_snapshot_manager, "yaml").fetch(
            core_client, ".storage/core.yaml::rest"
        )


async def test_a_yaml_target_without_a_key_has_nothing_to_back_up(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    assert (
        await _handler(file_snapshot_manager, "yaml").fetch(core_client, "no-separator")
        is None
    )


async def test_restoring_a_yaml_key_replaces_its_subtree(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await write_config_files(
        hass, {"configuration.yaml": "rest:\n  - resource: http://changed\n"}
    )

    await _handler(file_snapshot_manager, "yaml").restore(
        core_client, "configuration.yaml::rest", "- resource: http://a\n"
    )

    assert "http://a" in _read(hass, "configuration.yaml")
    assert "http://changed" not in _read(hass, "configuration.yaml")


async def test_a_file_path_containing_the_separator_splits_on_the_last_one(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """The (file, key) pair stays correct for a path that itself has ``::``."""
    await write_config_files(
        hass, {"packages/weird::name.yaml": "rest:\n  - resource: http://changed\n"}
    )

    await _handler(file_snapshot_manager, "yaml").restore(
        core_client, "packages/weird::name.yaml::rest", "- resource: http://a\n"
    )

    assert "http://a" in _read(hass, "packages/weird::name.yaml")


async def test_a_yaml_restore_with_no_key_is_refused(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    with pytest.raises(ValueError):
        await _handler(file_snapshot_manager, "yaml").restore(
            core_client, "no-separator", "x"
        )


@pytest.fixture
def extra_key(
    backup_settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator allowed ``alert2`` as a YAML key the tools may write."""
    monkeypatch.setattr(backup_settings, "extra_yaml_write_keys", "alert2")


async def test_a_snapshot_of_an_operator_extra_key_can_be_restored(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    extra_key: None,
) -> None:
    """The write that produced the snapshot was allowed only because of the
    extra-key setting, so the restore has to carry the same set or the
    component rejects it as not in the allowed list (#1887)."""
    await write_config_files(
        hass, {"packages/alerts.yaml": "alert2:\n  defaults: {}\n"}
    )

    await _handler(file_snapshot_manager, "yaml").restore(
        core_client, "packages/alerts.yaml::alert2", "defaults:\n  severity: low\n"
    )

    assert "severity: low" in _read(hass, "packages/alerts.yaml")


async def test_without_the_setting_an_extra_key_is_not_restorable(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    await write_config_files(
        hass, {"packages/alerts.yaml": "alert2:\n  defaults: {}\n"}
    )

    with pytest.raises((ToolError, HomeAssistantError)):
        await _handler(file_snapshot_manager, "yaml").restore(
            core_client, "packages/alerts.yaml::alert2", "defaults:\n  severity: low\n"
        )

    assert "severity" not in _read(hass, "packages/alerts.yaml")


async def _older_component(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The File & YAML Tools component reports version 1.2.3, from before
    ``extra_allowed_keys`` existed."""
    integration = await async_get_integration(hass, DOMAIN)
    monkeypatch.setitem(integration.manifest, "version", "1.2.3")


async def test_a_component_too_old_for_extra_keys_blocks_the_restore_with_an_update_prompt(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    extra_key: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same actionable prompt as the write path, not the opaque "extra keys
    not allowed" schema rejection from Home Assistant."""
    await write_config_files(
        hass, {"packages/alerts.yaml": "alert2:\n  defaults: {}\n"}
    )
    await _older_component(hass, monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await _handler(file_snapshot_manager, "yaml").restore(
            core_client, "packages/alerts.yaml::alert2", "defaults:\n  severity: low\n"
        )

    assert "1.2.4" in str(excinfo.value)
    assert "severity" not in _read(hass, "packages/alerts.yaml")


async def test_a_default_configuration_still_restores_on_an_older_component(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no extra keys the payload is what it was before #1887, so a component
    that predates the field restores."""
    await write_config_files(
        hass, {"configuration.yaml": "rest:\n  - resource: http://changed\n"}
    )
    await _older_component(hass, monkeypatch)

    await _handler(file_snapshot_manager, "yaml").restore(
        core_client, "configuration.yaml::rest", "- resource: http://a\n"
    )

    assert "http://a" in _read(hass, "configuration.yaml")


async def test_a_whole_yaml_file_is_snapshotted_as_text_and_registered(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await write_config_files(hass, {"configuration.yaml": "a: 1\n"})

    captured = await file_snapshot_manager.maybe_snapshot(
        "yaml_file", "configuration.yaml", force=True
    )

    assert captured is not None
    held: Any = file_snapshot_manager.read_snapshot(captured.name)
    assert held["kind"] == "text"
    assert held["config"] == "a: 1\n"
    assert "yaml_file" in file_snapshot_manager.supported_domains()


async def test_restoring_a_whole_yaml_file_replaces_it_verbatim(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """``write_file`` rejects config files, so the whole-file restore goes
    through the YAML edit service's ``replace_file``."""
    await write_config_files(hass, {"configuration.yaml": "a: changed\n"})

    await _handler(file_snapshot_manager, "yaml_file").restore(
        core_client, "configuration.yaml", "a: 1\n"
    )

    assert _read(hass, "configuration.yaml") == "a: 1\n"


async def test_a_failed_whole_file_restore_is_raised(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    with pytest.raises(HomeAssistantError):
        await _handler(file_snapshot_manager, "yaml_file").restore(
            core_client, "not_a_config_file.yaml", "a: 1\n"
        )
