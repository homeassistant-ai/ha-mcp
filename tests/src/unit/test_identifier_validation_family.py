"""A blank identifier is refused by name instead of reaching Home Assistant.

``validate_identifier_not_empty`` refuses ``None``, ``""`` and whitespace with
``VALIDATION_INVALID_PARAMETER`` naming the parameter. Without it a call site
routes ``label_id=""`` to create instead of update, or sends ``"   "`` to Home
Assistant and reports its misleading "not found". Each call site is called on
real Core with a whitespace identifier, the value a bare ``if not value``
check lets through; where ``None`` means "create" or "list", that path is
shown to still work.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.helpers import validate_identifier_not_empty
from ha_mcp.tools.tools_addons import register_addon_tools
from ha_mcp.tools.tools_areas import AreaTools
from ha_mcp.tools.tools_calendar import CalendarTools
from ha_mcp.tools.tools_categories import CategoryTools
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_groups import GroupTools
from ha_mcp.tools.tools_hacs import HacsTools
from ha_mcp.tools.tools_integrations import IntegrationTools
from ha_mcp.tools.tools_labels import LabelTools
from ha_mcp.tools.tools_registry import RegistryTools
from ha_mcp.tools.tools_resources import ResourceTools
from ha_mcp.tools.tools_todo import TodoTools
from ha_mcp.tools.tools_zones import ZoneTools

from ._config_writes import automation_tools
from ._tool_errors import error_body, rejected_call_body


class TestValidateIdentifierNotEmptyHelper:
    @pytest.mark.parametrize(
        "bad",
        [None, "", " \t\n\r\v", "\xa0　"],
        ids=["none", "empty", "ascii_whitespace", "unicode_whitespace"],
    )
    def test_missing_or_blank_identifier_is_refused_by_name(
        self, bad: str | None
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(bad, "test_param")
        body = error_body(excinfo)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["parameter"] == "test_param"

    @pytest.mark.parametrize("good", [" abc ", "0"])
    def test_real_identifier_is_returned_untouched(self, good: str) -> None:
        assert validate_identifier_not_empty(good, "test_param") == good

    def test_caller_context_and_suggestions_reach_the_error(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(
                "  ",
                "label_id",
                suggestions=["Omit label_id to create a new label"],
                context={"action": "set", "name": "Critical"},
            )
        body = error_body(excinfo)
        assert body["action"] == "set"
        assert body["name"] == "Critical"
        assert "Omit label_id" in body["error"]["suggestion"]

    def test_message_override_replaces_the_default_text(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(
                "", "name", message="name is required when creating a new area"
            )
        assert (
            error_body(excinfo)["error"]["message"]
            == "name is required when creating a new area"
        )

    def test_caller_context_cannot_hide_the_refused_parameter(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(
                "", "label_id", context={"parameter": "imposter", "value": "imposter"}
            )
        body = error_body(excinfo)
        assert (body["parameter"], body["value"]) == ("label_id", "")


Call = Callable[[HomeAssistantClient, str], Awaitable[Any]]
# "" slips past a truthiness route (``"update" if x else "create"``), "   " past
# an ``if not x`` check.
BLANKS = [pytest.param("", id="empty"), pytest.param("   ", id="whitespace")]
AUTOMATION = {"alias": "x", "trigger": [], "action": []}
EVENT = {
    "summary": "Renamed",
    "start": "2026-06-15T10:00:00",
    "end": "2026-06-15T11:00:00",
}


def _case(call: Call, parameter: str, *domains: str, name: str) -> Any:
    return pytest.param(call, parameter, domains, id=name)


async def _manage_app(client: HomeAssistantClient, **arguments: Any) -> Any:
    mcp = FastMCP("apps")
    register_addon_tools(mcp, client)
    tool = await mcp.get_tool("ha_manage_app")
    return await tool.fn(**arguments)


BLANK_IDENTIFIER_CALLS = [
    _case(
        lambda c, v: LabelTools(c).ha_config_set_label(name="X", label_id=v),
        "label_id",
        name="set_label",
    ),
    _case(
        lambda c, v: LabelTools(c).ha_config_get_label(label_id=v),
        "label_id",
        name="get_label",
    ),
    _case(
        lambda c, v: LabelTools(c).ha_config_remove_label(label_id=v),
        "label_id",
        name="remove_label",
    ),
    _case(
        lambda c, v: CategoryTools(c).ha_config_set_category(
            name="X", scope="automation", category_id=v
        ),
        "category_id",
        name="set_category",
    ),
    _case(
        lambda c, v: CategoryTools(c).ha_config_get_category(
            scope="automation", category_id=v
        ),
        "category_id",
        name="get_category",
    ),
    _case(
        lambda c, v: CategoryTools(c).ha_config_remove_category(
            scope="automation", category_id=v
        ),
        "category_id",
        name="remove_category",
    ),
    _case(
        lambda c, v: AreaTools(c).ha_set_area_or_floor(kind="area", id=v, name="X"),
        "id",
        name="update_area_or_floor",
    ),
    _case(
        lambda c, v: AreaTools(c).ha_set_area_or_floor(kind="area", name=v),
        "name",
        name="create_area",
    ),
    _case(
        lambda c, v: AreaTools(c).ha_set_area_or_floor(kind="floor", name=v),
        "name",
        name="create_floor",
    ),
    _case(
        lambda c, v: AreaTools(c).ha_remove_area_or_floor(kind="area", id=v),
        "id",
        name="remove_area_or_floor",
    ),
    _case(
        lambda c, v: HelperConfigTools(c).ha_config_set_helper(
            helper_type="input_boolean", action="update", helper_id=v, name="X"
        ),
        "helper_id",
        "input_boolean",
        name="update_helper",
    ),
    _case(
        lambda c, v: HelperConfigTools(c).ha_config_set_helper(
            helper_type="input_boolean", helper_id=v, name="X"
        ),
        "helper_id",
        "input_boolean",
        name="helper_without_action",
    ),
    _case(
        lambda c, v: ResourceTools(c).ha_config_set_dashboard_resource(
            url="/local/test.js", resource_type="module", resource_id=v
        ),
        "resource_id",
        "lovelace",
        name="set_resource",
    ),
    _case(
        lambda c, v: ResourceTools(c).ha_config_delete_dashboard_resource(
            resource_id=v
        ),
        "resource_id",
        "lovelace",
        name="delete_resource",
    ),
    _case(
        lambda c, v: ZoneTools(c).ha_set_zone(zone_id=v, name="X"),
        "zone_id",
        "zone",
        name="set_zone",
    ),
    _case(
        lambda c, v: ZoneTools(c).ha_remove_zone(zone_id=v),
        "zone_id",
        "zone",
        name="remove_zone",
    ),
    _case(
        lambda c, v: AutomationConfigTools(c).ha_config_remove_automation(entity_id=v),
        "entity_id",
        name="remove_automation",
    ),
    _case(
        lambda c, v: AutomationConfigTools(c).ha_config_get_automation(entity_id=v),
        "entity_id",
        name="get_automation",
    ),
    _case(
        lambda c, v: AutomationConfigTools(c).ha_config_set_automation(
            config=AUTOMATION, entity_id=v
        ),
        "entity_id",
        name="update_automation",
    ),
    _case(
        lambda c, v: ConfigScriptTools(c).ha_config_remove_script(script_id=v),
        "script_id",
        name="remove_script",
    ),
    _case(
        lambda c, v: ConfigScriptTools(c).ha_config_get_script(script_id=v),
        "script_id",
        name="get_script",
    ),
    _case(
        lambda c, v: ConfigScriptTools(c).ha_config_set_script(
            script_id=v, config={"sequence": [{"delay": {"seconds": 1}}]}
        ),
        "script_id",
        name="set_script",
    ),
    _case(
        lambda c, v: DashboardConfigTools(c).ha_config_get_dashboard(url_path=v),
        "url_path",
        "lovelace",
        name="get_dashboard",
    ),
    _case(
        lambda c, v: DashboardConfigTools(c).ha_config_set_dashboard(
            url_path=v, config={"views": []}
        ),
        "url_path",
        "lovelace",
        name="set_dashboard",
    ),
    _case(
        lambda c, v: DashboardConfigTools(c).ha_config_delete_dashboard(url_path=v),
        "url_path",
        "lovelace",
        name="delete_dashboard",
    ),
    _case(
        lambda c, v: GroupTools(c).ha_config_remove_group(object_id=v),
        "object_id",
        name="remove_group",
    ),
    _case(
        lambda c, v: GroupTools(c).ha_config_set_group(
            object_id=v, entities=["light.example"]
        ),
        "object_id",
        name="set_group",
    ),
    _case(
        lambda c, v: IntegrationTools(c).ha_remove_helpers_integrations(
            target=v, confirm=True
        ),
        "target",
        name="remove_helper_or_integration",
    ),
    _case(
        lambda c, v: IntegrationTools(c).ha_set_integration(entry_id=v, enabled=False),
        "entry_id",
        name="enable_integration",
    ),
    _case(
        lambda c, v: IntegrationTools(c).ha_set_integration(
            entry_id=v, reconfigure=True, config={"host": "h"}
        ),
        "entry_id",
        name="reconfigure_integration",
    ),
    _case(
        lambda c, v: IntegrationTools(c).ha_set_integration(domain=v),
        "domain",
        name="add_integration",
    ),
    _case(
        lambda c, v: CalendarTools(c).ha_config_remove_calendar_event(
            entity_id="calendar.family", uid=v
        ),
        "uid",
        name="remove_calendar_event",
    ),
    _case(
        lambda c, v: CalendarTools(c).ha_config_set_calendar_event(
            entity_id="calendar.family", uid=v, **EVENT
        ),
        "uid",
        name="update_calendar_event",
    ),
    _case(
        lambda c, v: TodoTools(c).ha_remove_todo_item(
            entity_id="todo.shopping_list", item=v
        ),
        "item",
        name="remove_todo_item",
    ),
    _case(
        lambda c, v: TodoTools(c).ha_set_todo_item(
            entity_id="todo.shopping_list", item=v
        ),
        "item",
        name="update_todo_item",
    ),
    _case(
        lambda c, v: EntityTools(c).ha_remove_entity(entity_id=v),
        "entity_id",
        name="remove_entity",
    ),
    _case(
        lambda c, v: EntityTools(c).ha_set_entity(
            entity_id=["sensor.real", v], categories={"automation": "cat_id"}
        ),
        "entity_id",
        name="set_entities",
    ),
    _case(
        lambda c, v: RegistryTools(c).ha_remove_device(device_id=v),
        "device_id",
        name="remove_device",
    ),
    _case(
        lambda c, v: RegistryTools(c).ha_set_device(device_id=v, name="New Name"),
        "device_id",
        name="set_device",
    ),
    _case(
        lambda c, v: _manage_app(c, slug=v, path="/api/health"),
        "slug",
        name="manage_app",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(action="download", repository_id=v),
        "repository_id",
        name="hacs_download",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(action="remove", repository_id=v),
        "repository_id",
        name="hacs_remove",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(
            action="update_information", repository_id=v
        ),
        "repository_id",
        name="hacs_update_information",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_get_hacs_info(action="info", repository_id=v),
        "repository_id",
        name="hacs_info",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(
            action="add_repository", repository=v, category="integration"
        ),
        "repository",
        name="hacs_add_repository_without_repository",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(
            action="add_repository", repository="owner/repo", category=v
        ),
        "category",
        name="hacs_add_repository_without_category",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(
            action="remove_repository", repository_id=v
        ),
        "repository_id",
        name="hacs_remove_repository",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(
            action="set_version", repository_id="123", version=v
        ),
        "version",
        name="hacs_set_version_without_version",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_manage_hacs(
            action="acknowledge_critical", repository=v
        ),
        "repository",
        name="hacs_acknowledge_critical",
    ),
    _case(
        lambda c, v: HacsTools(c).ha_get_hacs_info(action="detect", repository=v),
        "repository",
        name="hacs_detect",
    ),
]


@pytest.mark.parametrize(("call", "parameter", "domains"), BLANK_IDENTIFIER_CALLS)
@pytest.mark.parametrize("blank", BLANKS)
async def test_blank_identifier_is_refused_by_name(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    call: Call,
    parameter: str,
    domains: tuple[str, ...],
    blank: str,
) -> None:
    """A blank identifier must not route to another action or come back as
    Home Assistant's "not found" for an id the caller never meant."""
    for domain in ("config", *domains):
        assert await async_setup_component(hass, domain, {})

    body = await rejected_call_body(call(core_client, blank))

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER", body
    assert body.get("parameter") == parameter, body


@pytest.mark.parametrize("blank", BLANKS)
async def test_blank_helper_name_is_refused_on_create(
    hass: HomeAssistant, core_client: HomeAssistantClient, blank: str
) -> None:
    """Core would store a helper with a blank name."""
    for domain in ("config", "input_boolean"):
        assert await async_setup_component(hass, domain, {})

    body = await rejected_call_body(
        HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean", action="create", name=blank
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER", body
    assert body["error"]["message"].startswith("name "), body


@pytest.mark.parametrize(
    "other_mode",
    [{"domain": "shelly"}, {"enabled": False}],
    ids=["with_domain", "with_enabled"],
)
async def test_reconfigure_combined_with_another_mode_is_refused(
    core_client: HomeAssistantClient, other_mode: dict[str, Any]
) -> None:
    """Reconfigure changes one entry; a domain or enabled flag beside it would
    leave the caller unsure which mode ran."""
    body = await rejected_call_body(
        IntegrationTools(core_client).ha_set_integration(
            entry_id="abc", reconfigure=True, config={"host": "h"}, **other_mode
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER", body


def _fresh(name: str) -> str:
    return f"{name} {uuid.uuid4().hex[:8]}"


class TestOmittedIdentifierStillCreates:
    """``None`` stays the "create new" sentinel the blank guard must not catch."""

    @pytest.fixture(autouse=True)
    async def config(self, hass: HomeAssistant) -> None:
        assert await async_setup_component(hass, "config", {})

    async def test_label_without_id_is_created(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        name = _fresh("Label")
        result = await LabelTools(core_client).ha_config_set_label(name=name)

        assert result["success"] is True, result
        assert lr.async_get(hass).async_get_label_by_name(name) is not None

    async def test_category_without_id_is_created(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        name = _fresh("Category")
        result = await CategoryTools(core_client).ha_config_set_category(
            name=name, scope="automation"
        )

        assert result["success"] is True, result
        created = cr.async_get(hass).async_list_categories(scope="automation")
        assert name in [category.name for category in created]

    async def test_area_without_id_is_created(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        name = _fresh("Area")
        result = await AreaTools(core_client).ha_set_area_or_floor(
            kind="area", name=name
        )

        assert result["success"] is True, result
        assert ar.async_get(hass).async_get_area_by_name(name) is not None

    async def test_floor_without_id_is_created(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        name = _fresh("Floor")
        result = await AreaTools(core_client).ha_set_area_or_floor(
            kind="floor", name=name
        )

        assert result["success"] is True, result
        assert fr.async_get(hass).async_get_floor_by_name(name) is not None

    async def test_dashboard_resource_without_id_is_created(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "lovelace", {})
        result = await ResourceTools(core_client).ha_config_set_dashboard_resource(
            url="/local/test.js", resource_type="module"
        )

        assert result["success"] is True, result
        listed = await core_client.send_websocket_message(
            {"type": "lovelace/resources"}
        )
        assert [r["url"] for r in listed["result"]] == ["/local/test.js"]
        assert result["resource_id"] == listed["result"][0]["id"]

    async def test_zone_without_id_is_created(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "zone", {})
        name = _fresh("Office")
        result = await ZoneTools(core_client).ha_set_zone(
            name=name, latitude=40.7128, longitude=-74.0060, radius=150
        )

        assert result["success"] is True, result
        listed = await core_client.send_websocket_message({"type": "zone/list"})
        assert name in [z["name"] for z in listed["result"]]

    async def test_automation_without_id_is_created(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools = await automation_tools(hass, core_client, monkeypatch)
        result = await tools.ha_config_set_automation(config=AUTOMATION)

        assert result["success"] is True, result
        assert hass.states.get(result["entity_id"]) is not None, result


class TestOmittedDashboardStillReads:
    @pytest.fixture(autouse=True)
    async def lovelace(self, hass: HomeAssistant) -> None:
        assert await async_setup_component(hass, "lovelace", {})

    async def test_dashboard_without_url_path_reads_the_default_dashboard(
        self, core_client: HomeAssistantClient
    ) -> None:
        saved = await core_client.send_websocket_message(
            {"type": "lovelace/config/save", "config": {"views": [{"title": "Home"}]}}
        )
        assert saved["success"], saved

        result = await DashboardConfigTools(core_client).ha_config_get_dashboard()

        assert result["success"] is True, result
        assert result["config"]["views"] == [{"title": "Home"}], result

    async def test_listing_dashboards_needs_no_url_path(
        self, core_client: HomeAssistantClient
    ) -> None:
        created = await core_client.send_websocket_message(
            {
                "type": "lovelace/dashboards/create",
                "url_path": "test-board",
                "title": "Test Board",
            }
        )
        assert created["success"], created

        result = await DashboardConfigTools(core_client).ha_config_get_dashboard(
            list_only=True
        )

        assert result["success"] is True, result
        assert "test-board" in str(result), result
