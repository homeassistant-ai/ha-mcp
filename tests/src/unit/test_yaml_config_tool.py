"""ha_config_set_yaml on a real ``hass`` with the File & YAML Tools entry set up.

The tool calls the component's ``edit_yaml_config`` service through the
server's client, and the component edits files in Core's config directory, a
temporary one of the test's own. Edits apply in one call unless a test turns
the confirmation step on; every write takes its mandatory snapshot under
``tmp_path``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api.constants import WS_DASHBOARDS
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.exceptions import HomeAssistantConnectionError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_yaml_config import YAML_PACKAGES_FLAG_BY_KEY, YamlConfigTools

from ._backup_core import backup_settings as backup_settings
from ._command_faults import dropped_on, pooled_socket, received
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._core_setup import set_up_tools_entry

DASHBOARDS_LIST = "lovelace/dashboards/list"
CONFIGURATION = "homeassistant:\n  name: Home\n"
DASHBOARD = "mode: yaml\ntitle: Energy\nfilename: dashboards/energy.yaml\n"
TEMPLATE = "- sensor:\n    - name: Doubled\n      state: '{{ 2 * 2 }}'\n"
COMMAND_LINE = "- sensor:\n    name: Uptime\n    command: uptime\n"
UTILITY_METER = "monthly_energy:\n  source: sensor.energy\n  cycle: monthly\n"
THEME = "primary-color: '#000000'\n"
PACKAGE_CONTENT = {
    "automation": "- alias: Lights\n  triggers: []\n  actions: []\n",
    "script": "lights:\n  sequence: []\n",
    "scene": "- name: Evening\n  entities: {}\n",
}


@pytest.fixture
async def tool(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> YamlConfigTools:
    """The tool on a ``hass`` with the File & YAML Tools entry: one-call edits,
    every package key allowed, no extra write keys."""
    monkeypatch.setattr(backup_settings, "enable_yaml_edit_confirm", False)
    monkeypatch.setattr(backup_settings, "extra_yaml_write_keys", "")
    for flag in YAML_PACKAGES_FLAG_BY_KEY.values():
        monkeypatch.setattr(backup_settings, flag, True)
    await write_config_files(hass, {"configuration.yaml": CONFIGURATION})
    await set_up_tools_entry(hass)
    return YamlConfigTools(core_client)


async def _read(hass: HomeAssistant, name: str) -> str:
    return await hass.async_add_executor_job(Path(hass.config.path(name)).read_text)


async def _loaded(hass: HomeAssistant, name: str) -> Any:
    return yaml.safe_load(await _read(hass, name))


async def _exists(hass: HomeAssistant, name: str) -> bool:
    return await hass.async_add_executor_job(Path(hass.config.path(name)).exists)


def _yaml_files(folder: Path) -> list[Path]:
    return sorted(folder.rglob("*.yaml"))


def _indented(text: str, depth: int) -> str:
    return "".join(f"{' ' * depth}{line}\n" for line in text.splitlines())


async def _storage_dashboard(client: HomeAssistantClient, url_path: str) -> None:
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": "Energy",
            "mode": "storage",
        }
    )
    assert created["success"], created


def _helper_advice(result: dict[str, Any]) -> list[str]:
    return [w for w in result.get("warnings", []) if "ha_config_set_helper" in w]


# ---------------------------------------------------------------------------
# YAML dashboards and the storage dashboards Core already serves
# ---------------------------------------------------------------------------


async def test_dashboard_a_storage_dashboard_already_serves_is_not_added(
    hass: HomeAssistant, tool: YamlConfigTools, core_client: HomeAssistantClient
) -> None:
    """Two dashboards on one url_path: Home Assistant would refuse the YAML one
    at the next restart."""
    assert await async_setup_component(hass, "lovelace", {})
    await _storage_dashboard(core_client, "energy-dash")

    with pytest.raises(ToolError) as refused:
        await tool.ha_config_set_yaml(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content=DASHBOARD,
        )

    assert "A storage-mode dashboard already owns url_path 'energy-dash'" in str(
        refused.value
    )
    assert await _read(hass, "configuration.yaml") == CONFIGURATION


async def test_dashboard_is_added_beside_storage_dashboards_on_other_url_paths(
    hass: HomeAssistant, tool: YamlConfigTools, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    await _storage_dashboard(core_client, "other-dash")

    with received(hass, WS_DASHBOARDS, DASHBOARDS_LIST) as seen:
        result = await tool.ha_config_set_yaml(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content=DASHBOARD,
        )

    assert result["written"] is True
    written = await _loaded(hass, "configuration.yaml")
    assert written["lovelace"]["dashboards"]["energy-dash"] == yaml.safe_load(DASHBOARD)
    assert seen[WS_DASHBOARDS] or seen[DASHBOARDS_LIST]


async def test_yaml_dashboard_on_the_same_url_path_is_not_a_collision(
    hass: HomeAssistant, tool: YamlConfigTools
) -> None:
    """Rewriting an existing YAML dashboard's entry is an edit, not a second
    dashboard."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": "lovelace:\n  dashboards:\n    energy-dash:\n"
            + _indented("mode: yaml\ntitle: Old\nfilename: dashboards/old.yaml", 6)
        },
    )
    await setup_from_configuration(hass, "lovelace")

    result = await tool.ha_config_set_yaml(
        yaml_path="lovelace.dashboards.energy-dash",
        action="replace",
        content=DASHBOARD,
    )

    assert result["written"] is True
    written = await _loaded(hass, "configuration.yaml")
    assert written["lovelace"]["dashboards"]["energy-dash"]["title"] == "Energy"


async def test_dashboard_lookup_that_gets_no_answer_does_not_block_the_write(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The lookup only guards against a collision; a dropped connection must
    not stop the user registering a dashboard."""
    assert await async_setup_component(hass, "lovelace", {})
    socket = await pooled_socket(core_client)

    with (
        dropped_on(hass, WS_DASHBOARDS, socket),
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ),
    ):
        result = await tool.ha_config_set_yaml(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content=DASHBOARD,
        )

    assert result["written"] is True
    assert (
        "energy-dash"
        in (await _loaded(hass, "configuration.yaml"))["lovelace"]["dashboards"]
    )
    assert (
        "lovelace/dashboards/list WS query failed (connection lost); "
        "skipping collision check"
    ) in caplog.text


async def test_yaml_dashboard_entry_is_removable_while_a_storage_dashboard_owns_its_url_path(
    hass: HomeAssistant, tool: YamlConfigTools, core_client: HomeAssistantClient
) -> None:
    """Migrating a dashboard to storage mode ends with removing its YAML entry."""
    assert await async_setup_component(hass, "lovelace", {})
    await _storage_dashboard(core_client, "energy-dash")
    await write_config_files(
        hass,
        {
            "configuration.yaml": CONFIGURATION
            + "lovelace:\n  dashboards:\n    energy-dash:\n"
            + _indented(DASHBOARD, 6)
        },
    )

    result = await tool.ha_config_set_yaml(
        yaml_path="lovelace.dashboards.energy-dash", action="remove"
    )

    assert result["written"] is True
    assert await _read(hass, "configuration.yaml") == CONFIGURATION


async def test_edit_of_another_key_makes_no_dashboards_lookup(
    hass: HomeAssistant, tool: YamlConfigTools
) -> None:
    assert await async_setup_component(hass, "lovelace", {})

    with received(hass, WS_DASHBOARDS, DASHBOARDS_LIST) as seen:
        result = await tool.ha_config_set_yaml(
            yaml_path="template", action="add", content=TEMPLATE
        )

    assert result["written"] is True
    assert seen == {WS_DASHBOARDS: [], DASHBOARDS_LIST: []}


# ---------------------------------------------------------------------------
# The per-key settings for automation / script / scene in package files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "file", "action"),
    [
        ("automation", "packages/automation.yaml", "add"),
        ("script", "packages/script.yaml", "add"),
        ("scene", "packages/scene.yaml", "add"),
        ("automation", "./packages/automation.yaml", "add"),
        ("scene", "packages/scene.yaml", "remove"),
    ],
)
async def test_package_key_turned_off_is_refused_with_how_to_turn_it_on(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    file: str,
    action: str,
) -> None:
    """Each key has its own setting, and the refusal names the one to turn on."""
    monkeypatch.setattr(backup_settings, YAML_PACKAGES_FLAG_BY_KEY[key], False)
    package = yaml.safe_dump({key: yaml.safe_load(PACKAGE_CONTENT[key])})
    await write_config_files(hass, {file: package})

    with pytest.raises(ToolError) as refused:
        await tool.ha_config_set_yaml(
            file=file,
            yaml_path=key,
            action=action,
            content=None if action == "remove" else PACKAGE_CONTENT[key],
        )

    assert f"Enable 'Allow {key} in packages/*.yaml'" in str(refused.value)
    assert await _read(hass, file) == package


async def test_package_key_settings_hold_in_a_package_folder_of_the_users_naming(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant loads packages from whatever folder configuration.yaml
    names, so a key turned off stays off there too."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": "homeassistant:\n  packages: !include_dir_named rooms\n"
        },
    )
    monkeypatch.setattr(backup_settings, "enable_yaml_packages_scene", False)

    with pytest.raises(ToolError) as refused:
        await tool.ha_config_set_yaml(
            file="rooms/evening.yaml",
            yaml_path="scene",
            action="add",
            content=PACKAGE_CONTENT["scene"],
        )
    applied = await tool.ha_config_set_yaml(
        file="rooms/lights.yaml",
        yaml_path="automation",
        action="add",
        content=PACKAGE_CONTENT["automation"],
    )

    assert "yaml_path key 'scene' is disabled" in str(refused.value)
    assert not await _exists(hass, "rooms/evening.yaml")
    assert applied["written"] is True
    assert (await _loaded(hass, "rooms/lights.yaml"))["automation"] == yaml.safe_load(
        PACKAGE_CONTENT["automation"]
    )


async def test_other_keys_are_written_with_every_package_key_turned_off(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for flag in YAML_PACKAGES_FLAG_BY_KEY.values():
        monkeypatch.setattr(backup_settings, flag, False)

    result = await tool.ha_config_set_yaml(
        yaml_path="template", action="add", content=TEMPLATE
    )

    assert result["written"] is True
    assert (await _loaded(hass, "configuration.yaml"))["template"] == yaml.safe_load(
        TEMPLATE
    )


async def test_package_only_key_in_configuration_yaml_is_pointed_at_a_package_file(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With its setting off the key is still refused for being in the wrong
    file, so the user learns where it belongs, not which setting to change."""
    monkeypatch.setattr(backup_settings, "enable_yaml_packages_automation", False)

    with pytest.raises(ToolError) as refused:
        await tool.ha_config_set_yaml(
            yaml_path="automation", action="add", content=PACKAGE_CONTENT["automation"]
        )

    assert "is only allowed in packages/*.yaml files, not in configuration.yaml" in str(
        refused.value
    )
    assert await _read(hass, "configuration.yaml") == CONFIGURATION


# ---------------------------------------------------------------------------
# Advice to use a storage-mode helper instead (#1720)
# ---------------------------------------------------------------------------


async def test_key_a_helper_can_replace_points_at_the_helper_tool(
    tool: YamlConfigTools,
) -> None:
    result = await tool.ha_config_set_yaml(
        yaml_path="utility_meter", action="add", content=UTILITY_METER
    )

    assert result["written"] is True
    assert any(
        "ha_config_set_helper(helper_type='utility_meter')" in w
        for w in _helper_advice(result)
    )


@pytest.mark.parametrize(
    ("file", "yaml_path", "action", "content"),
    [
        pytest.param(
            "configuration.yaml", "command_line", "add", COMMAND_LINE, id="no-helper"
        ),
        pytest.param("themes/group.yaml", "group", "add", THEME, id="theme-name"),
        pytest.param(
            "configuration.yaml", "utility_meter", "remove", None, id="removal"
        ),
    ],
)
async def test_no_helper_advice_where_no_helper_replaces_the_edit(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    file: str,
    yaml_path: str,
    action: str,
    content: str | None,
) -> None:
    """A key no helper covers, a theme that happens to share a helper's name,
    and a removal draw no advice to create a helper."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": CONFIGURATION
            + "utility_meter:\n"
            + _indented(UTILITY_METER, 2)
        },
    )

    result = await tool.ha_config_set_yaml(
        file=file, yaml_path=yaml_path, action=action, content=content
    )

    assert result["written"] is True
    assert _helper_advice(result) == []


# ---------------------------------------------------------------------------
# The two-step confirmation (#1720)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("confirm", "written"), [(True, False), (False, True)])
async def test_confirmation_setting_decides_whether_the_first_call_writes(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    confirm: bool,
    written: bool,
) -> None:
    monkeypatch.setattr(backup_settings, "enable_yaml_edit_confirm", confirm)

    result = await tool.ha_config_set_yaml(
        yaml_path="template", action="add", content=TEMPLATE
    )

    assert result["written"] is written
    assert ("template" in await _loaded(hass, "configuration.yaml")) is written


async def test_confirm_token_from_the_preview_applies_the_edit(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "enable_yaml_edit_confirm", True)
    preview = await tool.ha_config_set_yaml(
        yaml_path="template", action="add", content=TEMPLATE
    )

    result = await tool.ha_config_set_yaml(
        yaml_path="template",
        action="add",
        content=TEMPLATE,
        confirm_token=preview["confirm_token"],
    )

    assert result["written"] is True
    assert (await _loaded(hass, "configuration.yaml"))["template"] == yaml.safe_load(
        TEMPLATE
    )


async def test_confirm_leg_leaves_out_the_skill_guidance_the_preview_carried(
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller read the guidance with the preview; repeating it on the
    confirm leg only costs tokens."""
    monkeypatch.setattr(backup_settings, "enable_yaml_edit_confirm", True)
    preview = await tool.ha_config_set_yaml(
        yaml_path="command_line", action="add", content=COMMAND_LINE
    )

    applied = await tool.ha_config_set_yaml(
        yaml_path="command_line",
        action="add",
        content=COMMAND_LINE,
        confirm_token=preview["confirm_token"],
    )

    assert preview["skill_content"]
    assert applied["written"] is True
    assert "skill_content" not in applied


async def test_preview_takes_no_backup_and_the_confirm_leg_does(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preview writes nothing, so a snapshot for it would list an edit that
    never happened among the user's backups."""
    monkeypatch.setattr(backup_settings, "enable_yaml_edit_confirm", True)
    await write_config_files(
        hass,
        {"configuration.yaml": "command_line:\n" + _indented(COMMAND_LINE, 2)},
    )
    backups = Path(backup_settings.auto_backup_dir)

    preview = await tool.ha_config_set_yaml(
        yaml_path="command_line", action="replace", content="[]\n"
    )
    after_preview = await asyncio.to_thread(_yaml_files, backups)
    await tool.ha_config_set_yaml(
        yaml_path="command_line",
        action="replace",
        content="[]\n",
        confirm_token=preview["confirm_token"],
    )

    assert after_preview == []
    assert len(await asyncio.to_thread(_yaml_files, backups)) == 1


# ---------------------------------------------------------------------------
# Themes, extra write keys, and what an edit leaves alone
# ---------------------------------------------------------------------------


async def test_theme_reload_failure_is_reported_beside_the_written_theme(
    hass: HomeAssistant, tool: YamlConfigTools
) -> None:
    """The file is written but Home Assistant still shows the old theme; the
    user needs to know the reload did not happen."""
    result = await tool.ha_config_set_yaml(
        file="themes/dark.yaml", yaml_path="dark", action="add", content=THEME
    )

    assert (await _loaded(hass, "themes/dark.yaml")) == {"dark": yaml.safe_load(THEME)}
    failure = (
        f"frontend.reload_themes failed: {ServiceNotFound('frontend', 'reload_themes')}"
    )
    assert any(failure in w for w in result["warnings"])


async def test_key_the_operator_allowed_is_writable(
    hass: HomeAssistant,
    tool: YamlConfigTools,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting is free text: spaces, empty entries and repeats are what an
    operator types."""
    monkeypatch.setattr(
        backup_settings, "extra_yaml_write_keys", " alert2 , , foo,alert2 "
    )

    result = await tool.ha_config_set_yaml(
        yaml_path="foo", action="add", content="defaults: {}\n"
    )

    assert result["written"] is True
    assert (await _loaded(hass, "configuration.yaml"))["foo"] == {"defaults": {}}


async def test_key_off_the_allowlist_is_refused(
    hass: HomeAssistant, tool: YamlConfigTools
) -> None:
    with pytest.raises(ToolError) as refused:
        await tool.ha_config_set_yaml(
            yaml_path="foo", action="add", content="defaults: {}\n"
        )

    assert "Key 'foo' is not in the allowed list" in str(refused.value)
    assert await _read(hass, "configuration.yaml") == CONFIGURATION


async def test_remove_keeps_the_comment_block_above_the_next_key(
    hass: HomeAssistant, tool: YamlConfigTools
) -> None:
    """The tool promises to keep comments; a remove must take only the removed
    key's own lines, not the notes the user wrote above the key after it."""
    kept_head = "homeassistant:\n  name: Home\n\n"
    removed = (
        "command_line:\n  - sensor:\n      name: Uptime\n      command: uptime\n\n"
    )
    kept_tail = (
        "# Phone notifications\n"
        "# (set up by hand)\n"
        "notify:\n"
        "  - name: phone\n"
        "    platform: group\n"
        "    services: []\n"
    )
    await write_config_files(
        hass, {"configuration.yaml": kept_head + removed + kept_tail}
    )

    result = await tool.ha_config_set_yaml(yaml_path="command_line", action="remove")

    assert result["written"] is True
    assert await _read(hass, "configuration.yaml") == kept_head + kept_tail
