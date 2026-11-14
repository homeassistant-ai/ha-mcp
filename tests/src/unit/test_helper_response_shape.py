"""Write tools put their payload and warnings where callers look (#1293, #1332).

``ha_config_set_helper`` returns its post-write payload under ``data`` on the
create, update and flow-helper paths, and every warning as a string in the
top-level ``warnings`` list, never as a singular ``warning`` or nested in the
payload. The lifecycle write tools (groups, scripts, automations, scenes) and
the tools swept under #1341 keep the same warnings list.

Every reply is Core's: the tools run on the server's own client connected to a
real ``hass``. A failure is made in Core at the moment the tool reaches it
(``_receipt_faults``): an entity removed while its registry update is on the
way, the client's state reads losing their answer or its token revoked when a
wait starts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.homeassistant import async_set_stop_handler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.backup_snapshot_restore import restore_backup
from ha_mcp.tools.tools_addons import register_addon_tools
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_energy import EnergyTools
from ha_mcp.tools.tools_groups import GroupTools
from ha_mcp.tools.tools_integrations import IntegrationTools
from ha_mcp.tools.tools_system import SystemTools

from ._config_dir_yaml import setup_scenes, write_config_files
from ._config_writes import automation_tools, script_tools
from ._core_setup import add_test_entry
from ._helper_tool_core import create_item, fresh_name, set_up, stored_item
from ._receipt_faults import at_entity_wait, lose_state_reads, on_receipt, revoke
from ._supervisor_core import load_supervisor_api

REGISTRY_UPDATE = "config/entity_registry/update"


def _assert_uniform_shape(result: dict[str, Any]) -> None:
    """``data`` holds the payload; no legacy wrapper; warnings stay top-level."""
    assert result["success"] is True
    assert isinstance(result["data"], dict), result
    assert "helper_data" not in result, "legacy 'helper_data' wrapper still present"
    assert "updated_data" not in result, "legacy 'updated_data' wrapper still present"
    assert "warning" not in result
    assert "warning" not in result["data"], result["data"]
    assert "warnings" not in result["data"], result["data"]
    if "warnings" in result:
        assert result["warnings"], "warnings list present but empty"
        assert all(isinstance(w, str) for w in result["warnings"])


def _assert_warnings_at_top_level(result: dict[str, Any]) -> None:
    """``warnings`` is a non-empty top-level ``list[str]``; no dict value nests a
    ``warnings`` or ``warning`` key. ``validation`` is the reference validator's
    own metadata (``merge_validation_meta``), outside this contract."""
    assert "warning" not in result, "singular 'warning' key at top level"
    warnings = result.get("warnings")
    assert isinstance(warnings, list) and warnings, result
    assert all(isinstance(w, str) for w in warnings), warnings
    for key, value in result.items():
        if key != "validation" and isinstance(value, dict):
            assert "warnings" not in value, f"warnings nested in {key!r}"
            assert "warning" not in value, f"'warning' nested in {key!r}"


def _assert_warnings_shape(result: dict[str, Any]) -> None:
    """``_assert_warnings_at_top_level`` for a tool that reports ``success``."""
    assert result["success"] is True
    _assert_warnings_at_top_level(result)


def _remove_entity(entity_registry: er.EntityRegistry) -> Callable[[Any], None]:
    """Remove the entity a registry update names before Core handles the update."""
    return lambda msg: entity_registry.async_remove(msg["entity_id"])


@pytest.fixture
async def helpers(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HelperConfigTools:
    """The helper tools on Core's storage input_booleans, without the component."""
    await set_up(hass, "input_boolean")
    return HelperConfigTools(core_client)


async def _boolean(
    client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> str:
    """An input_boolean Core stores; returns its entity_id."""
    item_id = await create_item(client, "input_boolean", {})
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", item_id
    )
    assert entity_id is not None
    return entity_id


def _category(hass: HomeAssistant) -> str:
    return cr.async_get(hass).async_create(scope="helpers", name="Test Cat").category_id


class TestUniformResponseShape:
    """#1293: create, update and flow paths return the same wrapper shape."""

    async def test_create_returns_the_stored_item_under_data(
        self, core_client: HomeAssistantClient, helpers: HelperConfigTools
    ) -> None:
        result = await helpers.ha_config_set_helper(
            helper_type="input_boolean", name=fresh_name()
        )

        _assert_uniform_shape(result)
        assert result["action"] == "create"
        stored = await stored_item(core_client, "input_boolean", result["data"]["id"])
        assert result["data"] == stored

    async def test_update_returns_the_updated_item_under_data(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        await set_up(hass, "input_select")
        item_id = await create_item(core_client, "input_select", {"options": ["a"]})
        entity_id = entity_registry.async_get_entity_id(
            "input_select", "input_select", item_id
        )

        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_select",
            helper_id=entity_id,
            config={"options": ["x", "y"]},
        )

        _assert_uniform_shape(result)
        assert "warnings" not in result
        assert result["action"] == "update"
        assert result["data"]["options"] == ["x", "y"]

    async def test_flow_helper_create_returns_entry_under_data(
        self, hass: HomeAssistant, helpers: HelperConfigTools
    ) -> None:
        """The entry is under ``data``; flat ``entry_id`` / ``title`` stay as the
        identifiers callers reach for."""
        hass.states.async_set("sensor.a", "1")
        hass.states.async_set("sensor.b", "2")

        result = await helpers.ha_config_set_helper(
            helper_type="min_max",
            name="avg_temp",
            config={"entity_ids": ["sensor.a", "sensor.b"], "type": "mean"},
        )

        _assert_uniform_shape(result)
        [entry] = hass.config_entries.async_entries("min_max")
        assert result["data"] == {"entry_id": entry.entry_id, "title": "avg_temp"}
        assert result["entry_id"] == entry.entry_id
        assert result["title"] == "avg_temp"
        assert result["entity_ids"] == ["sensor.avg_temp"]

    async def test_create_wait_failure_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        helpers: HelperConfigTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with at_entity_wait(hass, lambda: lose_state_reads(core_client, monkeypatch)):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean", name=fresh_name()
            )

        _assert_uniform_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_update_wait_failure_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        helpers: HelperConfigTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entity_id = await _boolean(core_client, entity_registry)

        with at_entity_wait(hass, lambda: lose_state_reads(core_client, monkeypatch)):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean", helper_id=entity_id, name=fresh_name()
            )

        _assert_uniform_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_create_failed_registry_update_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        area_registry: ar.AreaRegistry,
        entity_registry: er.EntityRegistry,
        helpers: HelperConfigTools,
    ) -> None:
        """The area that did not land is neither echoed in ``data`` nor nested."""
        area_id = area_registry.async_create("Kitchen").id

        with on_receipt(hass, REGISTRY_UPDATE, _remove_entity(entity_registry)):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean", name=fresh_name(), area_id=area_id
            )

        _assert_uniform_shape(result)
        assert any("entity registry update failed" in w for w in result["warnings"])
        assert any("Entity not found" in w for w in result["warnings"])
        assert "area_id" not in result["data"]

    async def test_create_echoes_icon_and_area_once_the_registry_has_them(
        self, area_registry: ar.AreaRegistry, helpers: HelperConfigTools
    ) -> None:
        """Without the component the icon goes to the entity registry, not the
        item, so only the registry write's success puts it in ``data``."""
        area_id = area_registry.async_create("Kitchen").id

        result = await helpers.ha_config_set_helper(
            helper_type="input_boolean",
            name=fresh_name(),
            icon="mdi:toggle-switch",
            area_id=area_id,
        )

        _assert_uniform_shape(result)
        assert result["action"] == "create"
        assert result["data"]["icon"] == "mdi:toggle-switch"
        assert result["data"]["area_id"] == area_id

    async def test_update_failed_registry_update_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        area_registry: ar.AreaRegistry,
        entity_registry: er.EntityRegistry,
        helpers: HelperConfigTools,
    ) -> None:
        area_id = area_registry.async_create("Kitchen").id
        entity_id = await _boolean(core_client, entity_registry)

        with on_receipt(hass, REGISTRY_UPDATE, _remove_entity(entity_registry)):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean",
                helper_id=entity_id,
                area_id=area_id,
                wait=False,
            )

        _assert_uniform_shape(result)
        assert any("entity registry update failed" in w for w in result["warnings"])
        assert any("Entity not found" in w for w in result["warnings"])
        assert "area_id" not in result["data"]

    async def test_create_failed_category_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        helpers: HelperConfigTools,
    ) -> None:
        """#1293 / #1355: the category warning is lifted to ``warnings``; neither
        it nor the category leaks into ``data``."""
        category = _category(hass)

        with on_receipt(hass, REGISTRY_UPDATE, _remove_entity(entity_registry)):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean", name=fresh_name(), category=category
            )

        _assert_uniform_shape(result)
        assert "category_warning" not in result["data"]
        assert "category" not in result["data"]
        assert any("failed to set category" in w for w in result["warnings"])

    async def test_update_failed_category_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        helpers: HelperConfigTools,
    ) -> None:
        category = _category(hass)
        entity_id = await _boolean(core_client, entity_registry)

        with on_receipt(hass, REGISTRY_UPDATE, _remove_entity(entity_registry)):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean",
                helper_id=entity_id,
                name=fresh_name(),
                category=category,
                wait=False,
            )

        _assert_uniform_shape(result)
        assert "category_warning" not in result["data"]
        assert "category" not in result["data"]
        assert any("failed to set category" in w for w in result["warnings"])

    async def test_unqueryable_entity_is_a_warnings_entry_not_a_warning_key(
        self, hass: HomeAssistant, helpers: HelperConfigTools
    ) -> None:
        """The pre-#1293 ``helper_data["warning"]`` is ``warnings[i]`` now.

        The entity's state is gone when the wait starts, so the wait runs out.
        """

        def remove_new_state() -> None:
            [state] = hass.states.async_all("input_boolean")
            hass.states.async_remove(state.entity_id)

        with at_entity_wait(hass, remove_new_state):
            result = await helpers.ha_config_set_helper(
                helper_type="input_boolean", name=fresh_name()
            )

        _assert_uniform_shape(result)
        assert any("not yet queryable" in w for w in result["warnings"])


async def _group_tools(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> GroupTools:
    assert await async_setup_component(hass, "group", {})
    return GroupTools(client)


async def _scene_tools(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> ConfigSceneTools:
    await setup_scenes(hass, [])
    return ConfigSceneTools(client)


AUTOMATION = {
    "alias": "Test Auto",
    # An event trigger: a time trigger leaves Core's timer behind the test.
    "trigger": [{"platform": "event", "event_type": "test_event"}],
    "action": [{"service": "light.turn_on"}],
}
SCENE = {"name": "Test Scene", "entities": {"light.kitchen": {"state": "on"}}}


class Family(NamedTuple):
    """A lifecycle write tool family: its Core, its write and its removal."""

    set_up: Callable[
        [HomeAssistant, HomeAssistantClient, pytest.MonkeyPatch], Awaitable[Any]
    ]
    write: Callable[[Any], Awaitable[dict[str, Any]]]
    remove: Callable[[Any], Awaitable[dict[str, Any]]]


FAMILIES = {
    "groups": Family(
        _group_tools,
        lambda t: t.ha_config_set_group(
            object_id="test_group", entities=["light.kitchen"]
        ),
        lambda t: t.ha_config_remove_group(object_id="test_group"),
    ),
    "scripts": Family(
        script_tools,
        lambda t: t.ha_config_set_script(
            script_id="test_script", config={"sequence": [{"delay": {"seconds": 1}}]}
        ),
        lambda t: t.ha_config_remove_script(script_id="test_script"),
    ),
    "automations": Family(
        automation_tools,
        lambda t: t.ha_config_set_automation(config=AUTOMATION),
        lambda t: t.ha_config_remove_automation(entity_id="automation.test_auto"),
    ),
    "scenes": Family(
        _scene_tools,
        lambda t: t.ha_config_set_scene(scene_id="test_scene", config=SCENE),
        lambda t: t.ha_config_remove_scene(scene_id="test_scene"),
    ),
}

# The write of an existing item, for the families whose warning names it.
UPDATES: dict[str, Callable[[Any], Awaitable[dict[str, Any]]]] = {
    "groups": lambda t: t.ha_config_set_group(
        object_id="test_group", name="Renamed Test Group"
    ),
    "automations": lambda t: t.ha_config_set_automation(
        entity_id="automation.test_auto", config=AUTOMATION
    ),
}


class TestLifecycleWriteWarningsShape:
    """#1332: the lifecycle write families keep the warnings-list contract.

    Only HomeAssistantConnectionError and HomeAssistantAuthError leave
    ``wait_for_entity_registered`` for the set call sites (a timeout returns
    False, an API error is swallowed in ``ws_waiters``), so each set path meets
    both. A remove goes through ``verify_entity_removed``, which turns any
    failed check into a warning (its branches are tested in
    test_remove_helpers_wait_warnings.py), so one failure pins the wiring.
    """

    @staticmethod
    async def _set_with_failed_wait(
        hass: HomeAssistant,
        client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        family: str,
        fault: Callable[[], object],
    ) -> None:
        tools = await FAMILIES[family].set_up(hass, client, monkeypatch)

        with at_entity_wait(hass, fault):
            result = await FAMILIES[family].write(tools)

        _assert_warnings_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    @pytest.mark.parametrize("family", FAMILIES)
    async def test_set_wait_without_answer_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        family: str,
    ) -> None:
        await self._set_with_failed_wait(
            hass,
            core_client,
            monkeypatch,
            family,
            lambda: lose_state_reads(core_client, monkeypatch),
        )

    # Core closes the revoked socket while its own close is cancelled, so
    # aiohttp's heartbeat timer on that server socket is never cancelled.
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    @pytest.mark.parametrize("family", FAMILIES)
    async def test_set_wait_with_revoked_token_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        hass_access_token: str,
        monkeypatch: pytest.MonkeyPatch,
        family: str,
    ) -> None:
        """Core answers the wait's state read with 401."""
        await self._set_with_failed_wait(
            hass,
            core_client,
            monkeypatch,
            family,
            lambda: revoke(hass, hass_access_token),
        )

    @pytest.mark.parametrize("family", FAMILIES)
    async def test_remove_check_failure_is_a_top_level_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        family: str,
    ) -> None:
        tools = await FAMILIES[family].set_up(hass, core_client, monkeypatch)
        await FAMILIES[family].write(tools)

        with at_entity_wait(hass, lambda: lose_state_reads(core_client, monkeypatch)):
            result = await FAMILIES[family].remove(tools)

        _assert_warnings_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    @pytest.mark.parametrize("family", UPDATES)
    async def test_update_wait_failure_says_updated(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        family: str,
    ) -> None:
        """An existing item is an update: a regression hardcoding "created" on
        the update path fails here."""
        tools = await FAMILIES[family].set_up(hass, core_client, monkeypatch)
        await FAMILIES[family].write(tools)

        with at_entity_wait(hass, lambda: lose_state_reads(core_client, monkeypatch)):
            result = await UPDATES[family](tools)

        _assert_warnings_shape(result)
        assert any("updated but" in w for w in result["warnings"])
        assert not any("created but" in w for w in result["warnings"])


async def _restarts(hass: HomeAssistant) -> AsyncMock:
    """Core's restart service with its config check; the stop it ends with is
    Core's stop-handler hook, so the test's Core records it instead of stopping."""
    await write_config_files(hass, {"configuration.yaml": ""})
    for domain in ("homeassistant", "config"):
        assert await async_setup_component(hass, domain, {})
    stop = AsyncMock()
    async_set_stop_handler(hass, stop)
    return stop


class TestSweepWarningsShape:
    """#1341: the non-lifecycle tools migrated under #1332.

    ``ha_search``'s legacy fallback and ``ha_set_entity``'s nested
    ``device_rename`` warnings are covered in their own tool tests
    (test_tools_search.py, test_entity_rename.py).
    """

    async def test_backup_restore_warnings_shape(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        stop = await _restarts(hass)
        assert await async_setup_component(hass, "backup", {})
        await hass.services.async_call("backup", "create", blocking=True)
        info = await core_client.send_websocket_message({"type": "backup/info"})
        [backup] = info["result"]["backups"]

        result = await restore_backup(core_client, backup["backup_id"])

        _assert_warnings_shape(result)
        stop.assert_awaited_once_with(hass, True)

    async def test_system_restart_warnings_shape(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
        stop = await _restarts(hass)

        result = await SystemTools(core_client).ha_restart(confirm=True)

        _assert_warnings_shape(result)
        stop.assert_awaited_once_with(hass, True)

    async def test_integration_ignored_device_id_warnings_shape(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """The single-entry payload has no top-level ``success``."""
        assert await async_setup_component(hass, "config", {})
        entry = add_test_entry(hass)

        result = await IntegrationTools(core_client).ha_get_integration(
            entry_id=entry.entry_id, device_id="dev_123", include_diagnostics=False
        )

        _assert_warnings_at_top_level(result)
        assert any("device_id" in w for w in result["warnings"])

    async def test_app_options_ignored_fields_warnings_shape(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Options mode answers ``status: "pending_restart"``, no ``success``."""
        load_supervisor_api(
            hass,
            aioclient_mock,
            monkeypatch,
            {
                ("get", "/addons/test_addon/info"): {
                    "options": {"log_level": "info"},
                    "schema": [{"name": "log_level", "required": False, "type": "str"}],
                },
                ("post", "/addons/test_addon/options"): {},
            },
        )
        # The server is not in an app: it reaches Supervisor through Core.
        monkeypatch.delenv("SUPERVISOR_TOKEN")
        registered: dict[str, Any] = {}
        mcp = MagicMock(
            tool=lambda *a, **k: lambda fn: registered.setdefault(fn.__name__, fn)
        )
        register_addon_tools(mcp, core_client)

        result = await registered["ha_manage_app"](
            slug="test_addon", options={"log_level": "debug", "zombie_field": "ghost"}
        )

        _assert_warnings_at_top_level(result)
        assert result["ignored_fields"] == ["zombie_field"]

    async def test_energy_preview_validate_failure_warnings_shape(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without the energy integration Core does not answer ``energy/validate``;
        the preview still reports ``success``."""
        result = await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [], "device_consumption": []},
            dry_run=True,
        )

        _assert_warnings_shape(result)
        assert any("Current-state validation failed" in w for w in result["warnings"])
