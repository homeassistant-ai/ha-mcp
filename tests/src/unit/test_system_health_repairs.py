"""``ha_get_system_health``'s repairs: Core's list, with Core's own strings.

The issues are created in Core's issue registry and the strings come from
Core's ``homeassistant`` translations, so every reply is Core's own.
"""

import re
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools

_UNFILLED_PLACEHOLDER = re.compile(r"\{\w+\}")


async def _set_up_repairs(
    hass: HomeAssistant, tmp_path: Path, *, strings: bool = True
) -> None:
    """Core's repairs, and the frontend that serves translations when ``strings``.

    The development-repo path keeps the frontend off its package files.
    """
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, "repairs", {})
    if strings:
        config = {"frontend": {"development_repo": str(tmp_path)}}
        assert await async_setup_component(hass, "frontend", config)


def _create_issue(hass: HomeAssistant, issue_id: str, **kwargs: Any) -> None:
    """A ``homeassistant`` issue whose title and description use placeholders."""
    ir.async_create_issue(
        hass,
        "homeassistant",
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
        translation_placeholders={"domain": "probe", "integration_title": "Probe"},
        **kwargs,
    )


async def _repairs(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    return await SystemTools(client).ha_get_system_health(include="repairs", **kwargs)


async def test_repairs_carry_the_title_and_description_the_repairs_page_shows(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_repairs(hass, tmp_path)
    _create_issue(hass, "deprecated_yaml_probe")

    result = await _repairs(core_client)

    (issue,) = result["repairs"]["issues"]
    for field in ("title", "description"):
        assert "Probe" in issue[field]
        assert not _UNFILLED_PLACEHOLDER.search(issue[field])
    assert issue["translation_placeholders"]["integration_title"] == "Probe"
    assert "dismissed_count" not in result["repairs"]


async def test_dismissed_repairs_are_left_out_unless_asked_for(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_repairs(hass, tmp_path)
    _create_issue(hass, "kept")
    _create_issue(hass, "dismissed")
    ir.async_ignore_issue(hass, "homeassistant", "dismissed", True)

    default = await _repairs(core_client)
    everything = await _repairs(core_client, include_dismissed_repairs=True)

    assert [i["issue_id"] for i in default["repairs"]["issues"]] == ["kept"]
    assert default["repairs"]["dismissed_count"] == 1
    assert {i["issue_id"] for i in everything["repairs"]["issues"]} == {
        "kept",
        "dismissed",
    }


async def test_a_disabled_entrys_repairs_stay_hidden_with_the_component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_repairs(hass, tmp_path)
    mock_integration(hass, MockModule("repairs_probe"))
    entry = MockConfigEntry(
        domain="repairs_probe", disabled_by=ConfigEntryDisabler.USER
    )
    entry.add_to_hass(hass)
    _create_issue(hass, "shown")
    _create_issue(hass, "hidden", config_entry_id=entry.entry_id)

    result = await _repairs(component_core_client)

    assert [i["issue_id"] for i in result["repairs"]["issues"]] == ["shown"]


async def test_repairs_stay_listed_with_a_warning_when_core_strings_are_unreadable(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_repairs(hass, tmp_path, strings=False)
    _create_issue(hass, "deprecated_yaml_probe")

    result = await _repairs(core_client)

    (issue,) = result["repairs"]["issues"]
    assert "title" not in issue
    assert any("repair strings" in warning for warning in result["warnings"])


async def test_a_failed_repairs_read_is_reported_in_the_section(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "system_health", {})

    result = await _repairs(core_client)

    assert result["repairs"]["error"]
    assert result["repairs"]["issues"] == []


async def test_repairs_carry_the_data_the_repairs_page_reads(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_repairs(hass, tmp_path)
    _create_issue(hass, "with_data", data={"flow_id": "reauth-flow"})
    _create_issue(hass, "without_data")

    result = await _repairs(core_client)

    data = {i["issue_id"]: i["data"] for i in result["repairs"]["issues"]}
    assert data == {"with_data": {"flow_id": "reauth-flow"}, "without_data": None}


async def test_a_repair_without_core_strings_is_named_in_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_repairs(hass, tmp_path)
    mock_integration(hass, MockModule("repairs_probe"))
    ir.async_create_issue(
        hass,
        "repairs_probe",
        "untranslated",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="missing",
    )

    result = await _repairs(core_client)

    (issue,) = result["repairs"]["issues"]
    assert "title" not in issue
    assert any("repairs_probe.untranslated" in w for w in result["warnings"])
