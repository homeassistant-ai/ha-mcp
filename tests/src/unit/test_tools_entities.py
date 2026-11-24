"""ha_set_entity and ha_remove_entity on a real ``hass``.

The registries, every reply and every refusal are Core's. A registry read
Core cannot be made to refuse is one Core function raising Core's own
exception; a dropped connection is the client getting no answer at all.
"""

from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import sent_types
from ._entity_tools_core import labels as labels
from ._entity_tools_core import registry_entity, stored_entry
from ._entity_tools_core import tools as tools
from ._tool_errors import error_body, error_of

_UPDATE = "config/entity_registry/update"
_GET = "config/entity_registry/get"
_LABELS = "config/label_registry/list"
_AREAS = "config/area_registry/list"


def _aliases(registry: er.EntityRegistry, entity_id: str) -> list[str | None]:
    """The stored aliases, with ``None`` for the entity's own name."""
    return [
        None if alias is er.COMPUTED_NAME else alias
        for alias in stored_entry(registry, entity_id).aliases
    ]


@pytest.fixture
def dropped(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> AsyncMock:
    """Every WebSocket command gets no answer: the connection drops."""
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    )
    return cast(AsyncMock, core_client.send_websocket_message)


class TestLabels:
    async def test_a_label_list_is_stored(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.test")

        result = await tools.ha_set_entity(entity_id="light.test", labels=labels)

        assert result["success"] is True
        assert f"labels={labels}" in result["updates"]
        assert stored_entry(entity_registry, "light.test").labels == set(labels)

    async def test_an_empty_label_list_clears_the_labels(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.test", labels=set(labels))

        await tools.ha_set_entity(entity_id="light.test", labels=[])

        assert stored_entry(entity_registry, "light.test").labels == set()

    async def test_a_json_array_of_labels_is_stored(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.test")

        await tools.ha_set_entity(entity_id="light.test", labels='["outdoor", "smart"]')

        assert stored_entry(entity_registry, "light.test").labels == set(labels)

    async def test_unparseable_labels_are_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.test", labels="not_json{")

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "labels" in error["message"]
        sent.assert_not_called()

    async def test_an_update_without_labels_keeps_the_labels(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.test", labels=set(labels))

        await tools.ha_set_entity(entity_id="light.test", name="New Name")

        stored = stored_entry(entity_registry, "light.test")
        assert stored.name == "New Name"
        assert stored.labels == set(labels)

    async def test_add_keeps_the_labels_already_there(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.test", labels={"outdoor"})

        await tools.ha_set_entity(
            entity_id="light.test", labels=["smart"], label_operation="add"
        )

        assert stored_entry(entity_registry, "light.test").labels == set(labels)

    async def test_remove_drops_only_the_named_labels(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.test", labels=set(labels))

        await tools.ha_set_entity(
            entity_id="light.test", labels=["smart"], label_operation="remove"
        )

        assert stored_entry(entity_registry, "light.test").labels == {"outdoor"}

    @pytest.mark.parametrize("label_operation", ["set", "add"])
    async def test_an_unknown_label_is_refused_before_the_write(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
        sent: AsyncMock,
        label_operation: str,
    ) -> None:
        registry_entity(entity_registry, "light.test", labels={"outdoor"})

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.test",
                labels=["ghost_label"],
                label_operation=label_operation,
            )

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["unknown_labels"] == ["ghost_label"]
        assert _UPDATE not in sent_types(sent)
        assert stored_entry(entity_registry, "light.test").labels == {"outdoor"}

    async def test_a_dangling_label_can_still_be_removed(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
        sent: AsyncMock,
    ) -> None:
        """Removing a label that is gone from the label registry is the cleanup
        path; checking it against that registry would block it."""
        registry_entity(
            entity_registry, "light.test", labels={"ghost_label", "outdoor"}
        )

        result = await tools.ha_set_entity(
            entity_id="light.test", labels=["ghost_label"], label_operation="remove"
        )

        assert result["success"] is True
        assert _LABELS not in sent_types(sent)
        assert stored_entry(entity_registry, "light.test").labels == {"outdoor"}


class TestRegistryReferences:
    async def test_an_unknown_area_is_refused_before_the_write(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        area_registry: ar.AreaRegistry,
        sent: AsyncMock,
    ) -> None:
        """An unknown area id would be stored as a dangling assignment."""
        area_registry.async_create("Living Room")
        registry_entity(entity_registry, "light.test")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.test", area_id="extérieur nord")

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["area_id"] == "extérieur nord"
        assert sent_types(sent) == [_AREAS]
        assert stored_entry(entity_registry, "light.test").area_id is None

    async def test_an_area_is_checked_once_then_written(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        area_registry: ar.AreaRegistry,
        sent: AsyncMock,
    ) -> None:
        area = area_registry.async_create("Living Room")
        registry_entity(entity_registry, "light.test")

        await tools.ha_set_entity(entity_id="light.test", area_id=area.id)

        assert sent_types(sent) == [_AREAS, _UPDATE]
        assert stored_entry(entity_registry, "light.test").area_id == area.id

    async def test_an_unknown_category_is_refused_in_its_own_scope(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        category_registry: cr.CategoryRegistry,
        sent: AsyncMock,
    ) -> None:
        category_registry.async_create(scope="automation", name="Lighting")
        registry_entity(entity_registry, "light.test")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.test", categories={"automation": "ghost_category"}
            )

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["category"] == "ghost_category"
        assert sent.call_args_list == [
            call({"type": "config/category_registry/list", "scope": "automation"})
        ]
        assert stored_entry(entity_registry, "light.test").categories == {}

    @pytest.mark.parametrize(
        ("registry", "method", "kwargs", "lookup"),
        [
            (ar.AreaRegistry, "async_list_areas", {"area_id": "living_room"}, _AREAS),
            (lr.LabelRegistry, "async_list_labels", {"labels": ["outdoor"]}, _LABELS),
            (
                cr.CategoryRegistry,
                "async_list_categories",
                {"categories": {"automation": "lighting"}},
                "config/category_registry/list",
            ),
        ],
        ids=["area", "labels", "categories"],
    )
    async def test_an_unreadable_registry_refuses_the_write(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        monkeypatch: pytest.MonkeyPatch,
        sent: AsyncMock,
        registry: type,
        method: str,
        kwargs: dict[str, Any],
        lookup: str,
    ) -> None:
        """Without the registry the reference cannot be checked, so nothing is
        written rather than a possibly dangling one."""
        registry_entity(entity_registry, "light.test")
        monkeypatch.setattr(
            registry,
            method,
            MagicMock(side_effect=HomeAssistantError("registry unavailable")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.test", **kwargs)

        assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
        assert sent_types(sent) == [lookup]

    async def test_a_dropped_area_read_refuses_the_write(
        self, tools: EntityTools, dropped: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.test", area_id="living_room")

        body = error_body(exc_info)
        assert body["error"]["code"] == "CONNECTION_FAILED"
        assert body["area_id"] == "living_room"
        assert dropped.call_args_list == [call({"type": _AREAS})]


class TestSingleEntityUpdate:
    async def test_a_name_update_sends_only_the_registry_update(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        sent: AsyncMock,
    ) -> None:
        """No exposure write is sent when expose_to is not given."""
        registry_entity(entity_registry, "light.test")

        result = await tools.ha_set_entity(entity_id="light.test", name="New Name")

        assert result["success"] is True
        assert sent_types(sent) == [_UPDATE]

    async def test_a_call_without_updates_is_refused(self, tools: EntityTools) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.test")

        assert "No updates specified" in error_of(exc_info)["message"]

    async def test_expose_to_as_a_list_is_refused(self, tools: EntityTools) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="light.test", expose_to=["conversation"]
            )

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_an_unknown_entity_is_cores_not_found_with_suggestions(
        self, tools: EntityTools, labels: list[str]
    ) -> None:
        """The caller is told how to find a valid entity id."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.nonexistent", labels=["outdoor"])

        error = error_of(exc_info)
        assert error["code"].endswith("_NOT_FOUND")
        assert "Entity not found" in error["message"]
        assert error.get("suggestion")

    async def test_a_device_rename_without_a_device_is_a_warning_not_partial(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "light.test")

        result = await tools.ha_set_entity(
            entity_id="light.test", new_device_name="Living Room"
        )

        assert result["success"] is True
        assert "partial" not in result
        assert any(
            "no associated device" in w for w in result["device_rename"]["warnings"]
        )


class TestDisableGuardrail:
    async def test_disabling_an_automation_points_to_its_own_switch(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="automation.test", enabled=False)

        error_text = str(exc_info.value)
        assert (
            "ha_config_set_automation(entity_id='automation.test', enabled=False)"
            in error_text
        )
        assert "ha_call_service('automation', 'turn_off'" not in error_text
        assert "re-enabled" not in error_text
        assert "reloaded" not in error_text
        suggestions = error_of(exc_info).get("suggestions", [])
        assert any(
            "ha_config_set_automation(entity_id=..., enabled=...)" in item
            for item in suggestions
        )
        assert not any("automation.turn_off" in item for item in suggestions)
        sent.assert_not_called()

    async def test_disabling_a_script_says_no_switch_exists(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="script.my_script", enabled=False)

        error_text = str(exc_info.value)
        assert (
            "ha_config_set_script(script_id='script.my_script', run='stop')"
            in error_text
        )
        assert "ha_call_service" not in error_text
        assert "re-enabled" not in error_text
        assert "reloaded" not in error_text
        assert "only to stop a currently running execution" in error_text
        assert "does not disable the script" in error_text
        assert "no script runtime enable/disable service" in error_text.lower()
        suggestions = error_of(exc_info).get("suggestions", [])
        assert not any(
            "Registry-level hiding is not available" in item for item in suggestions
        )
        assert not any("retry" in item.lower() for item in suggestions)
        sent.assert_not_called()

    async def test_disabling_an_automation_in_a_one_item_list_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id=["automation.test"], enabled=False)

        assert (
            "ha_config_set_automation(entity_id='automation.test', enabled=False)"
            in str(exc_info.value)
        )
        sent.assert_not_called()

    async def test_enabling_an_automation_is_written(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(
            entity_registry,
            "automation.test",
            disabled_by=er.RegistryEntryDisabler.USER,
        )

        result = await tools.ha_set_entity(entity_id="automation.test", enabled=True)

        assert result["success"] is True
        assert stored_entry(entity_registry, "automation.test").disabled_by is None

    async def test_disabling_another_domain_is_written(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "sensor.temperature")

        result = await tools.ha_set_entity(
            entity_id="sensor.temperature", enabled=False
        )

        assert result["success"] is True
        assert (
            stored_entry(entity_registry, "sensor.temperature").disabled_by
            is er.RegistryEntryDisabler.USER
        )


class TestRemoveEntity:
    async def test_a_dropped_removal_is_a_connection_failure(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        dropped: AsyncMock,
    ) -> None:
        registry_entity(entity_registry, "sensor.test_entity")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_remove_entity(entity_id="sensor.test_entity")

        assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
        assert dropped.call_args == call(
            {"type": "config/entity_registry/remove", "entity_id": "sensor.test_entity"}
        )
        assert entity_registry.async_get("sensor.test_entity") is not None

    async def test_cores_refusal_text_reaches_the_caller(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        registry_entity(entity_registry, "sensor.test_entity")
        monkeypatch.setattr(
            entity_registry,
            "async_remove",
            MagicMock(side_effect=HomeAssistantError("Permission denied")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_remove_entity(entity_id="sensor.test_entity")

        assert "permission denied" in error_of(exc_info)["message"].lower()


class TestShowAsAndOptions:
    async def test_device_class_is_the_show_as_field_not_an_option(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        """The UI's Show As reads device_class; an option would not show."""
        registry_entity(entity_registry, "binary_sensor.test")

        result = await tools.ha_set_entity(
            entity_id="binary_sensor.test", device_class="window"
        )

        stored = stored_entry(entity_registry, "binary_sensor.test")
        assert stored.device_class == "window"
        assert stored.options == {}
        assert "device_class='window'" in result["updates"]

    async def test_an_empty_device_class_clears_the_override(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "binary_sensor.test", device_class="door")

        result = await tools.ha_set_entity(
            entity_id="binary_sensor.test", device_class=""
        )

        assert stored_entry(entity_registry, "binary_sensor.test").device_class is None
        assert "device_class cleared" in result["updates"]

    async def test_options_alone_leave_the_other_fields_as_they_are(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        sent: AsyncMock,
    ) -> None:
        registry_entity(entity_registry, "sensor.temp", device_class="temperature")

        await tools.ha_set_entity(
            entity_id="sensor.temp", options={"sensor": {"display_precision": 2}}
        )

        stored = stored_entry(entity_registry, "sensor.temp")
        assert stored.options["sensor"] == {"display_precision": 2}
        assert stored.device_class == "temperature"
        assert sent_types(sent) == [_UPDATE]

    async def test_options_for_two_domains_are_all_stored(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "sensor.temp")
        options = {
            "sensor": {"display_precision": 1},
            "weather": {"forecast_type": "hourly"},
        }

        result = await tools.ha_set_entity(entity_id="sensor.temp", options=options)

        stored = stored_entry(entity_registry, "sensor.temp")
        assert {domain: stored.options[domain] for domain in options} == options
        assert result["entity_entry"]["options"] == dict(stored.options)

    @pytest.mark.parametrize("include_entity_entry", [True, False])
    async def test_a_refused_second_domain_says_what_was_written(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        monkeypatch: pytest.MonkeyPatch,
        include_entity_entry: bool,
    ) -> None:
        """A caller retrying blindly would apply the first domain twice; the
        entry stays out of a compact reply."""
        registry_entity(entity_registry, "sensor.temp")
        update_options = er.EntityRegistry.async_update_entity_options

        def refuse_weather(
            registry: er.EntityRegistry,
            entity_id: str,
            domain: str,
            options: Mapping[str, Any] | None,
        ) -> er.RegistryEntry:
            if domain == "weather":
                raise ValueError("Options refused")
            return update_options(registry, entity_id, domain, options)

        # Core answers a ValueError here with its invalid_info error.
        monkeypatch.setattr(
            er.EntityRegistry, "async_update_entity_options", refuse_weather
        )
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id="sensor.temp",
                options={
                    "sensor": {"display_precision": 1},
                    "weather": {"forecast_type": "hourly"},
                },
                include_entity_entry=include_entity_entry,
            )

        body = error_body(exc_info)
        assert ("entity_entry" in body) is include_entity_entry
        assert "weather" in body["error"]["message"]
        assert body["options_domain"] == "weather"
        assert body["partial"] is True
        assert body["options_succeeded"] == {"sensor": {"display_precision": 1}}
        stored = stored_entry(entity_registry, "sensor.temp")
        assert stored.options["sensor"] == {"display_precision": 1}
        assert "weather" not in stored.options

    @pytest.mark.parametrize(
        ("options", "named"),
        [
            (["not", "a", "dict"], "got list"),
            ({"sensor": "not-a-dict"}, "str"),
            ({}, "empty"),
        ],
        ids=["list", "inner-string", "empty"],
    )
    async def test_malformed_options_are_refused(
        self, tools: EntityTools, sent: AsyncMock, options: Any, named: str
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="sensor.temp", options=options)

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert named in error["message"].lower()
        sent.assert_not_called()

    async def test_device_class_and_options_are_both_stored(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "binary_sensor.test")

        await tools.ha_set_entity(
            entity_id="binary_sensor.test",
            device_class="window",
            options={"sensor": {"display_precision": 2}},
        )

        stored = stored_entry(entity_registry, "binary_sensor.test")
        assert stored.device_class == "window"
        assert stored.options["sensor"] == {"display_precision": 2}


class TestAliases:
    """Aliases keep the entity's own-name (``null``) entry (#2495)."""

    async def test_a_string_list_keeps_the_own_name(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "light.t", aliases=[er.COMPUTED_NAME])

        result = await tools.ha_set_entity(entity_id="light.t", aliases=["Maanlamp"])

        assert _aliases(entity_registry, "light.t") == [None, "Maanlamp"]
        assert "entity-name alias kept" in result["updates"]

    async def test_a_string_list_leaves_the_own_name_off(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "light.t", aliases=["old"])

        await tools.ha_set_entity(entity_id="light.t", aliases=["new"])

        assert _aliases(entity_registry, "light.t") == ["new"]

    async def test_clearing_the_aliases_keeps_the_own_name(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        """Otherwise the entity stops answering to its name in Assist."""
        registry_entity(entity_registry, "light.t", aliases=[er.COMPUTED_NAME, "old"])

        await tools.ha_set_entity(entity_id="light.t", aliases=[])

        assert _aliases(entity_registry, "light.t") == [None]

    @pytest.mark.parametrize(
        "aliases",
        [[None, "Maanlamp"], '[null, "Maanlamp"]'],
        ids=["list", "json-string"],
    )
    async def test_a_given_null_is_written_without_a_read(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        sent: AsyncMock,
        aliases: list[str | None] | str,
    ) -> None:
        registry_entity(entity_registry, "light.t", aliases=["old"])

        await tools.ha_set_entity(entity_id="light.t", aliases=aliases)

        assert sent_types(sent) == [_UPDATE]
        assert _aliases(entity_registry, "light.t") == [None, "Maanlamp"]

    async def test_a_refused_alias_read_writes_nothing(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        """Writing anyway could drop the entity's own name."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.t", aliases=["x"])

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "Entity not found" in error["message"]
        assert sent_types(sent) == [_GET]

    @pytest.mark.parametrize(
        ("switch", "expected", "note"),
        [
            (False, ["Maanlamp"], "entity-name alias off"),
            (True, [None, "Maanlamp"], "entity-name alias on"),
        ],
        ids=["off", "on"],
    )
    async def test_the_switch_with_aliases_needs_no_read(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        sent: AsyncMock,
        switch: bool,
        expected: list[str | None],
        note: str,
    ) -> None:
        """The switch decides the own-name entry, even one passed in aliases."""
        registry_entity(entity_registry, "light.t")
        given: list[str | None] = [None, "Maanlamp"] if not switch else ["Maanlamp"]

        result = await tools.ha_set_entity(
            entity_id="light.t", aliases=given, use_entity_name_alias=switch
        )

        assert sent_types(sent) == [_UPDATE]
        assert _aliases(entity_registry, "light.t") == expected
        assert note in result["updates"]

    @pytest.mark.parametrize(
        ("switch", "expected"),
        [(False, ["old"]), (True, [None, "old"])],
        ids=["off", "on"],
    )
    async def test_the_switch_alone_keeps_the_current_aliases(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        switch: bool,
        expected: list[str | None],
    ) -> None:
        registry_entity(entity_registry, "light.t", aliases=[er.COMPUTED_NAME, "old"])

        result = await tools.ha_set_entity(
            entity_id="light.t", use_entity_name_alias=switch
        )

        assert result["success"] is True
        assert _aliases(entity_registry, "light.t") == expected

    @pytest.mark.parametrize("aliases", [[1, "x"], '["x", 2]', "not json"])
    async def test_non_string_aliases_are_refused(
        self, tools: EntityTools, sent: AsyncMock, aliases: list[Any] | str
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id="light.t", aliases=aliases)

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        sent.assert_not_called()
