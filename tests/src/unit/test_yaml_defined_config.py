"""Automations and scripts defined in a package or an included YAML file.

Core's config API stores an automation in ``automations.yaml`` and a script in
``scripts.yaml`` and answers 404 for one defined anywhere else, although Core
runs it. The get tools return the definition Core loaded, say where it is
defined, and the write tools refuse it. Every test runs on a real ``hass``
whose configuration Core's own YAML loader read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import automation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_registration
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._command_faults import received
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._scripts_core import stored_scripts
from ._tool_errors import error_body

COMPONENT = "component"
FALLBACK = "fallback"
BOTH = pytest.mark.parametrize("routing", [COMPONENT, FALLBACK], indirect=True)
STORAGE = {"kind": "storage", "editable": True}

_FILES = {
    "configuration.yaml": (
        "homeassistant:\n"
        "  packages: !include_dir_named packages\n"
        "automation: !include automations.yaml\n"
        "automation split: !include_dir_merge_list automations\n"
        "script: !include scripts.yaml\n"
        "script split: !include_dir_merge_named scripts\n"
    ),
    "secrets.yaml": "door_code: hunter2-door\ndoor_codes:\n  front: hunter2-front\n",
    "automations.yaml": (
        "- id: '1712345678901'\n"
        "  alias: Stored\n"
        "  triggers: []\n"
        "  actions:\n"
        "    - event: stored_only\n"
    ),
    "scripts.yaml": "stored:\n  alias: Stored\n  sequence:\n    - event: stored_only\n",
    "packages/night.yaml": (
        "automation:\n"
        "  - id: packaged_night\n"
        "    alias: Packaged Night\n"
        "    triggers: []\n"
        "    actions:\n"
        "      - event: packaged_only\n"
        "        event_data:\n"
        "          code: !secret door_code\n"
        "          codes: !secret door_codes\n"
        "  - alias: Package No Id\n"
        "    triggers: []\n"
        "    actions:\n"
        "      - event: no_id_only\n"
        "  - id: packaged_motion\n"
        "    alias: Packaged Motion\n"
        "    use_blueprint:\n"
        "      path: homeassistant/motion_light.yaml\n"
        "      input:\n"
        "        motion_entity: binary_sensor.hall\n"
        "        light_target:\n"
        "          entity_id: light.hall\n"
        "script:\n"
        "  packaged_speakers:\n"
        "    alias: Packaged Speakers\n"
        "    sequence:\n"
        "      - event: packaged_script_only\n"
    ),
    "automations/office.yaml": (
        "- id: office_lights\n"
        "  alias: Office Lights\n"
        "  triggers: []\n"
        "  actions:\n"
        "    - event: office_only\n"
        "      event_data:\n"
        "        code: !secret office_code\n"
        "        pin: !secret office_pin\n"
        "        !secret office_key: open\n"
        "        !secret office_key_2: shut\n"
        "- id: office_keyed\n"
        "  alias: Office Keyed\n"
        "  triggers: []\n"
        "  actions:\n"
        "    - event: office_keyed_only\n"
        "      event_data:\n"
        "        !secret office_number_key: set\n"
    ),
    # Core resolves a !secret from the secrets.yaml nearest the file naming it.
    "automations/secrets.yaml": (
        "office_code: hunter2-office\noffice_pin: 471147\noffice_key: hunter2-key\n"
        "office_key_2: hunter2-key2\noffice_number_key: 864213\n"
    ),
    "scripts/office.yaml": (
        "office_join:\n  alias: Office Join\n  sequence:\n    - event: office_join_only\n"
    ),
}


@pytest.fixture
async def routing(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Core set up from the files above, with the component's commands unless
    the test asks for the fallback."""
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    # Core's own blueprint, which it copies into the config directory too late
    # for an automation its first load already uses.
    blueprint = Path(automation.__file__).parent / "blueprints" / "motion_light.yaml"
    motion_light = await hass.async_add_executor_job(blueprint.read_text)
    await write_config_files(
        hass,
        {
            **_FILES,
            "blueprints/automation/homeassistant/motion_light.yaml": motion_light,
        },
    )
    await setup_from_configuration(hass, "automation", "script")
    route: str = getattr(request, "param", COMPONENT)
    if route == COMPONENT:
        websocket_api.async_register_commands(hass)
    return route


@pytest.fixture
def automations(
    routing: str, core_client: HomeAssistantClient
) -> AutomationConfigTools:
    return AutomationConfigTools(core_client)


@pytest.fixture
def scripts(routing: str, core_client: HomeAssistantClient) -> ConfigScriptTools:
    return ConfigScriptTools(core_client)


async def _as_at_start(hass: HomeAssistant) -> None:
    """Date the secrets files before this process started, as on an instance
    whose secrets.yaml has not been touched since Home Assistant read it."""

    def age() -> None:
        for name in ("secrets.yaml", "automations/secrets.yaml"):
            os.utime(hass.config.path(name), (946684800, 946684800))

    await hass.async_add_executor_job(age)


def _yaml_source(routing: str, file: str, line: int) -> dict[str, Any]:
    known = routing == COMPONENT
    return {
        "kind": "yaml",
        "editable": False,
        "file": file if known else None,
        "line": line if known else None,
    }


class TestRead:
    @BOTH
    @pytest.mark.parametrize(
        ("target", "file", "line", "event"),
        [
            ("automation.packaged_night", "packages/night.yaml", 2, "packaged_only"),
            ("packaged_night", "packages/night.yaml", 2, "packaged_only"),
            ("automation.office_lights", "automations/office.yaml", 1, "office_only"),
        ],
        ids=["package-entity-id", "package-config-id", "included-directory"],
    )
    async def test_automation_outside_automations_yaml_reads_as_loaded(
        self,
        routing: str,
        automations: AutomationConfigTools,
        target: str,
        file: str,
        line: int,
        event: str,
    ) -> None:
        """A package or split-file automation that Home Assistant runs was
        reported as having no config, so an agent could not say what it does."""
        result = await automations.ha_config_get_automation(entity_id=target)

        assert result["config"]["actions"][0]["event"] == event
        assert result["source"] == _yaml_source(routing, file, line)
        assert result["entity"]["entity_id"].startswith("automation.")
        assert "cannot change it" in result["warnings"][0]
        assert any(COMPONENT_INSTALL_HINT in w for w in result["warnings"]) == (
            routing == FALLBACK
        )

    @BOTH
    async def test_automation_without_an_id_reads_by_its_entity_id(
        self, routing: str, automations: AutomationConfigTools
    ) -> None:
        """An automation with no ``id`` has no registry row, so it was
        reported as not existing."""
        result = await automations.ha_config_get_automation(
            entity_id="automation.package_no_id"
        )

        assert result["config"]["actions"] == [{"event": "no_id_only"}]
        assert result["source"]["kind"] == "yaml"
        assert result["entity"] is None

    async def test_blueprint_automation_names_its_own_file_not_the_blueprints(
        self, automations: AutomationConfigTools
    ) -> None:
        result = await automations.ha_config_get_automation(
            entity_id="automation.packaged_motion"
        )

        assert result["source"]["file"] == "packages/night.yaml"
        assert result["references"]["blueprint"] == "homeassistant/motion_light.yaml"

    async def test_secret_value_is_not_returned(
        self, automations: AutomationConfigTools
    ) -> None:
        """Core resolves ``!secret`` while loading, so the loaded definition
        holds the secret's value."""
        for entity_id in ("automation.packaged_night", "automation.office_lights"):
            result = await automations.ha_config_get_automation(entity_id=entity_id)

            assert "hunter2-" not in str(result), entity_id
            assert "**redacted**" in str(result), entity_id

    async def test_unreadable_secrets_file_is_said(
        self, hass: HomeAssistant, automations: AutomationConfigTools
    ) -> None:
        """With secrets.yaml unreadable nothing can be redacted; the body was
        returned as if it had been."""
        await write_config_files(hass, {"secrets.yaml": "door_code: [unclosed\n"})

        result = await automations.ha_config_get_automation(
            entity_id="automation.packaged_night"
        )

        assert any("may not be redacted" in w for w in result["warnings"])

    async def test_numeric_secret_changed_after_load_is_withheld_under_redaction(
        self, hass: HomeAssistant, automations: AutomationConfigTools, redact_on: None
    ) -> None:
        """A number carries no trace of the secrets file it came from, so once
        that file changed, the loaded number passed as the automation's own."""
        read = automations.ha_config_get_automation
        await _as_at_start(hass)
        assert "471147" not in str(await read(entity_id="automation.office_lights"))

        await write_config_files(
            hass, {"automations/secrets.yaml": "office_pin: 999999\n"}
        )

        with pytest.raises(ToolError) as exc:
            await read(entity_id="automation.office_lights")
        assert "471147" not in str(error_body(exc))

    async def test_numeric_secret_whose_file_was_deleted_is_withheld_under_redaction(
        self, hass: HomeAssistant, automations: AutomationConfigTools, redact_on: None
    ) -> None:
        """Once its secrets.yaml is gone, nothing names the loaded number as a
        secret; it was returned as the automation's own."""
        read = automations.ha_config_get_automation
        await _as_at_start(hass)
        assert "471147" not in str(await read(entity_id="automation.office_lights"))

        secrets = Path(hass.config.path("automations/secrets.yaml"))
        await hass.async_add_executor_job(secrets.unlink)

        with pytest.raises(ToolError) as exc:
            await read(entity_id="automation.office_lights")
        assert "471147" not in str(error_body(exc))

    async def test_numeric_secret_key_whose_file_was_deleted_is_withheld(
        self, hass: HomeAssistant, automations: AutomationConfigTools, redact_on: None
    ) -> None:
        """A number used as a mapping key was not counted as a number, so a
        definition whose only number was a secret key was returned in full."""
        read = automations.ha_config_get_automation
        await _as_at_start(hass)
        assert "864213" not in str(await read(entity_id="automation.office_keyed"))

        secrets = Path(hass.config.path("automations/secrets.yaml"))
        await hass.async_add_executor_job(secrets.unlink)

        with pytest.raises(ToolError) as exc:
            await read(entity_id="automation.office_keyed")
        assert "864213" not in str(error_body(exc))

    async def test_two_secret_keys_keep_both_entries(
        self, hass: HomeAssistant, automations: AutomationConfigTools
    ) -> None:
        """Two keys from secrets were both replaced by the same text, and the
        definition lost one of the entries."""
        result = await automations.ha_config_get_automation(
            entity_id="automation.office_lights"
        )

        data = result["config"]["actions"][0]["event_data"]
        assert "hunter2-" not in str(data)
        assert sorted(data.values()) == ["**redacted**", "**redacted**", "open", "shut"]

    async def test_secrets_file_that_contains_itself_is_read(
        self, hass: HomeAssistant, automations: AutomationConfigTools
    ) -> None:
        """A YAML alias can make a secret contain itself; collecting its values
        then never ended."""
        await write_config_files(
            hass, {"secrets.yaml": "door_code: hunter2-door\nloop: &loop [*loop]\n"}
        )

        result = await automations.ha_config_get_automation(
            entity_id="automation.packaged_night"
        )

        assert "hunter2-door" not in str(result)

    async def test_unreadable_secrets_file_withholds_under_redaction(
        self, hass: HomeAssistant, automations: AutomationConfigTools, redact_on: None
    ) -> None:
        await write_config_files(hass, {"secrets.yaml": "door_code: [unclosed\n"})

        with pytest.raises(ToolError) as exc:
            await automations.ha_config_get_automation(
                entity_id="automation.packaged_night"
            )

        assert "hunter2-" not in str(error_body(exc))
        assert "secrets.yaml" in error_body(exc)["error"]["message"]

    @BOTH
    @pytest.mark.parametrize(
        ("script_id", "key", "file", "line"),
        [
            (
                "script.packaged_speakers",
                "packaged_speakers",
                "packages/night.yaml",
                24,
            ),
            ("packaged_speakers", "packaged_speakers", "packages/night.yaml", 24),
            ("script.office_join", "office_join", "scripts/office.yaml", 2),
        ],
        ids=["package-entity-id", "package-key", "included-directory"],
    )
    async def test_script_outside_scripts_yaml_reads_as_loaded(
        self,
        routing: str,
        scripts: ConfigScriptTools,
        script_id: str,
        key: str,
        file: str,
        line: int,
    ) -> None:
        """A package or split-file script was reported as not found while its
        id was listed among the available scripts."""
        result = await scripts.ha_config_get_script(script_id=script_id)

        assert result["script_id"] == key
        assert result["config"]["sequence"][0]["event"].endswith("_only")
        assert result["source"] == _yaml_source(routing, file, line)
        assert "cannot change it" in result["warnings"][0]

    @BOTH
    async def test_stored_items_still_read_from_the_config_api(
        self,
        hass: HomeAssistant,
        routing: str,
        automations: AutomationConfigTools,
        scripts: ConfigScriptTools,
    ) -> None:
        """An item the UI stores must not be read from Core's loaded copy,
        which lags the stored one after a write, nor be called YAML."""
        with received(hass, "automation/config", "script/config") as loaded:
            automation = await automations.ha_config_get_automation(
                entity_id="automation.stored"
            )
            script = await scripts.ha_config_get_script(script_id="stored")

        assert automation["source"] == script["source"] == STORAGE
        for result in (automation, script):
            assert not any("YAML" in w for w in result.get("warnings", []))
        assert loaded == {"automation/config": [], "script/config": []}

    async def test_secret_changed_after_load_is_still_redacted(
        self, hass: HomeAssistant, automations: AutomationConfigTools
    ) -> None:
        """Core keeps the value it resolved at load; redacting by what
        secrets.yaml holds now returned the loaded value once the file changed."""
        await write_config_files(hass, {"secrets.yaml": "door_code: rotated\n"})

        result = await automations.ha_config_get_automation(
            entity_id="automation.packaged_night"
        )

        assert "hunter2-" not in str(result)

    @pytest.mark.parametrize("routing", [FALLBACK], indirect=True)
    async def test_without_the_component_redaction_withholds_the_definition(
        self, routing: str, automations: AutomationConfigTools, redact_on: None
    ) -> None:
        """Core's own command returns the loaded definition with its ``!secret``
        values resolved and unmarked, so they would leave with redaction on."""
        with pytest.raises(ToolError) as exc:
            await automations.ha_config_get_automation(
                entity_id="automation.packaged_night"
            )

        body = error_body(exc)
        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert "redact_secrets" in body["error"]["message"]
        assert "hunter2-" not in str(body)

    async def test_removed_script_core_still_holds_is_not_found(
        self, hass: HomeAssistant, scripts: ConfigScriptTools
    ) -> None:
        """Between a removal and the end of its reload, Core still holds the
        script it loaded from scripts.yaml; that is not a YAML-defined one."""
        await write_config_files(hass, {"scripts.yaml": "{}\n"})

        with pytest.raises(ToolError) as exc:
            await scripts.ha_config_get_script(script_id="stored")

        assert error_body(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestWrite:
    @BOTH
    @pytest.mark.parametrize("script_id", ["packaged_speakers", "script.office_join"])
    async def test_script_write_is_refused_not_stored_a_second_time(
        self,
        hass: HomeAssistant,
        routing: str,
        scripts: ConfigScriptTools,
        script_id: str,
    ) -> None:
        """Writing stored a second script under the same key in scripts.yaml,
        and Home Assistant then refused the package's script section."""
        with pytest.raises(ToolError) as exc:
            await scripts.ha_config_set_script(
                script_id=script_id,
                config={"alias": "Changed", "sequence": [{"event": "changed"}]},
                wait=False,
            )

        body = error_body(exc)
        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["source"]["kind"] == "yaml"
        assert list(await stored_scripts(hass)) == ["stored"]

    async def test_script_removal_says_where_the_script_is_defined(
        self, hass: HomeAssistant, scripts: ConfigScriptTools
    ) -> None:
        with pytest.raises(ToolError) as exc:
            await scripts.ha_config_remove_script(
                script_id="packaged_speakers", wait=False
            )

        body = error_body(exc)
        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["source"]["file"] == "packages/night.yaml"
        assert hass.states.get("script.packaged_speakers") is not None

    async def test_new_script_is_still_created(
        self, hass: HomeAssistant, scripts: ConfigScriptTools
    ) -> None:
        result = await scripts.ha_config_set_script(
            script_id="brand_new",
            config={"alias": "New", "sequence": [{"event": "new_only"}]},
            wait=False,
        )

        assert result["operation"] == "created"
        assert "brand_new" in await stored_scripts(hass)

    async def test_automation_write_names_the_file(
        self, automations: AutomationConfigTools
    ) -> None:
        with pytest.raises(ToolError) as exc:
            await automations.ha_config_set_automation(
                entity_id="automation.packaged_night",
                config={"alias": "Changed", "triggers": [], "actions": []},
                wait=False,
            )

        body = error_body(exc)
        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["source"] == _yaml_source(COMPONENT, "packages/night.yaml", 2)

    async def test_write_to_a_disabled_yaml_script_is_refused(
        self,
        hass: HomeAssistant,
        scripts: ConfigScriptTools,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Core loads nothing for a disabled entity, so a disabled package
        script looked like a new one and was stored a second time."""
        entity_registry.async_update_entity(
            "script.packaged_speakers", disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()

        with pytest.raises(ToolError) as exc:
            await scripts.ha_config_set_script(
                script_id="packaged_speakers",
                config={"alias": "Changed", "sequence": [{"event": "changed"}]},
                wait=False,
            )

        assert error_body(exc)["error"]["code"] == "CONFIG_NOT_FOUND"
        assert list(await stored_scripts(hass)) == ["stored"]
