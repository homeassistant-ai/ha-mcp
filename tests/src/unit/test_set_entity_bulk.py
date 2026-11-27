"""ha_set_entity with a list of entity_id, and its compact reply, on a real
``hass``: the registries and every write's answer are Core's.
"""

import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandNotSent
from ha_mcp.tools.tools_entities import EntityTools

from ._command_faults import dropped_on, pooled_socket
from ._entity_registry_core import sent_types
from ._entity_tools_core import labels as labels
from ._entity_tools_core import registry_entity, stored_entry
from ._entity_tools_core import tools as tools
from ._tool_errors import error_of

_LABELS = "config/label_registry/list"
_UPDATE = "config/entity_registry/update"
_GET = "config/entity_registry/get"


class TestBulk:
    async def test_labels_are_stored_on_every_entity(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        ids = ["light.a", "light.b", "light.c"]
        for entity_id in ids:
            registry_entity(entity_registry, entity_id)

        result = await tools.ha_set_entity(entity_id=ids, labels=["outdoor"])

        assert result["success"] is True
        assert (result["total"], result["succeeded_count"]) == (3, 3)
        assert result["failed_count"] == 0
        for entity_id in ids:
            assert stored_entry(entity_registry, entity_id).labels == {"outdoor"}

    @pytest.mark.parametrize(
        "single_entity_param",
        [
            {"name": "Test Name"},
            {"device_class": "window"},
            {"options": {"sensor": {"display_precision": 2}}},
            {"use_entity_name_alias": False},
        ],
        ids=["name", "device_class", "options", "use_entity_name_alias"],
    )
    @pytest.mark.parametrize(
        "ids", [["light.a", "light.b"], ["light.a", "light.a"]], ids=["two", "repeat"]
    )
    async def test_a_single_entity_parameter_is_refused(
        self,
        tools: EntityTools,
        sent: AsyncMock,
        single_entity_param: dict[str, Any],
        ids: list[str],
    ) -> None:
        """A bulk update would drop it silently, so it is refused; a list
        repeating one id is still a list."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id=ids,
                labels=["outdoor"],
                **single_entity_param,
            )

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert next(iter(single_entity_param)) in error["message"]
        sent.assert_not_called()

    async def test_an_entity_core_refuses_fails_alone(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        registry_entity(entity_registry, "light.a")

        result = await tools.ha_set_entity(
            entity_id=["light.a", "light.b"], labels=["outdoor"]
        )

        assert result["success"] is False
        assert result["partial"] is True
        assert [row["entity_id"] for row in result["succeeded"]] == ["light.a"]
        [failed] = result["failed"]
        assert failed["entity_id"] == "light.b"
        assert failed["ha_error_code"] == "not_found"
        assert stored_entry(entity_registry, "light.a").labels == {"outdoor"}

    async def test_an_empty_entity_list_is_refused(self, tools: EntityTools) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id=[], labels=["outdoor"])

        assert "empty" in error_of(exc_info)["message"].lower()

    async def test_a_list_without_any_change_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(entity_id=["light.a", "light.b"])

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        sent.assert_not_called()

    async def test_add_keeps_each_entitys_own_labels(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        label_registry: lr.LabelRegistry,
    ) -> None:
        for name in ("Existing", "Other", "New Label"):
            label_registry.async_create(name)
        registry_entity(entity_registry, "light.a", labels={"existing"})
        registry_entity(entity_registry, "light.b", labels={"other"})

        result = await tools.ha_set_entity(
            entity_id=["light.a", "light.b"],
            labels=["new_label"],
            label_operation="add",
        )

        assert result["succeeded_count"] == 2
        entries = {row["entity_id"]: row["entity_entry"] for row in result["succeeded"]}
        assert set(entries["light.a"]["labels"]) == {"existing", "new_label"}
        assert set(entries["light.b"]["labels"]) == {"other", "new_label"}
        assert stored_entry(entity_registry, "light.b").labels == {"other", "new_label"}

    async def test_a_category_is_stored_on_every_entity(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        category_registry: cr.CategoryRegistry,
    ) -> None:
        category = category_registry.async_create(scope="automation", name="Lights")
        ids = ["light.a", "light.b"]
        for entity_id in ids:
            registry_entity(entity_registry, entity_id)

        result = await tools.ha_set_entity(
            entity_id=ids, categories={"automation": category.category_id}
        )

        assert result["succeeded_count"] == 2
        for entity_id in ids:
            assert stored_entry(entity_registry, entity_id).categories == {
                "automation": category.category_id
            }

    @pytest.mark.parametrize(
        ("change", "field", "value"),
        [
            ({"hidden": True}, "hidden_by", er.RegistryEntryHider.USER),
            ({"enabled": False}, "disabled_by", er.RegistryEntryDisabler.USER),
        ],
        ids=["hidden", "enabled"],
    )
    async def test_hidden_or_enabled_alone_reaches_every_entity(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        change: dict[str, bool],
        field: str,
        value: object,
    ) -> None:
        """hidden or enabled as the only change must not be dropped from the
        registry work of a list."""
        ids = ["light.a", "light.b"]
        for entity_id in ids:
            registry_entity(entity_registry, entity_id)

        result = await tools.ha_set_entity(entity_id=ids, **change)

        assert result["succeeded_count"] == 2
        for entity_id in ids:
            assert getattr(stored_entry(entity_registry, entity_id), field) is value

    async def test_each_enabled_row_keeps_cores_restart_answer(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        """A bulk enable must not drop what Core says the enable still needs."""
        ids = ["light.a", "light.b"]
        for entity_id in ids:
            registry_entity(
                entity_registry, entity_id, disabled_by=er.RegistryEntryDisabler.USER
            )

        result = await tools.ha_set_entity(entity_id=ids, enabled=True)

        # Entries without a config entry: Core asks for a restart.
        assert [row.get("require_restart") for row in result["succeeded"]] == [
            True,
            True,
        ]

    async def test_a_compact_bulk_reply_leaves_out_every_entry(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
    ) -> None:
        """A large bulk reply must fit the client: the full entries stay out."""
        ids = ["light.a", "light.b"]
        for entity_id in ids:
            registry_entity(entity_registry, entity_id)

        result = await tools.ha_set_entity(
            entity_id=ids, labels=["outdoor"], include_entity_entry=False
        )

        assert [row["entity_id"] for row in result["succeeded"]] == ids
        assert not any("entity_entry" in row for row in result["succeeded"])
        assert all(row["updates"] for row in result["succeeded"])
        for entity_id in ids:
            assert stored_entry(entity_registry, entity_id).labels == {"outdoor"}

    @pytest.mark.parametrize("repeat", ["light.a", "LIGHT.A"])
    async def test_a_repeated_id_is_updated_and_reported_once(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
        sent: AsyncMock,
        repeat: str,
    ) -> None:
        """One write and one outcome per entity, whatever the case of the
        repeat: Core lowercases an entity_id."""
        for entity_id in ("light.a", "light.b"):
            registry_entity(entity_registry, entity_id)

        result = await tools.ha_set_entity(
            entity_id=["light.a", "light.b", repeat], labels=["outdoor"]
        )

        assert (result["total"], result["succeeded_count"]) == (2, 2)
        assert [row["entity_id"] for row in result["succeeded"]] == [
            "light.a",
            "light.b",
        ]
        assert sent_types(sent).count("config/entity_registry/update") == 2

    @pytest.mark.parametrize(
        ("fault", "change", "may_have_landed"),
        [
            ("update_dropped", {"hidden": True}, True),
            ("update_not_sent", {"hidden": True}, False),
            ("read_dropped", {"labels": ["outdoor"], "label_operation": "add"}, False),
        ],
    )
    async def test_a_lost_connection_says_which_updates_may_have_landed(
        self,
        hass: HomeAssistant,
        tools: EntityTools,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        labels: list[str],
        monkeypatch: pytest.MonkeyPatch,
        fault: str,
        change: dict[str, Any],
        may_have_landed: bool,
    ) -> None:
        """A lost connection fails each row with the transport's code, as it
        fails a single call; only an update sent without an answer may have
        been written, and only its row says so. A hidden change sends the
        update first; a label add reads the labels before it."""
        ids = ["light.a", "light.b"]
        for entity_id in ids:
            registry_entity(entity_registry, entity_id)
        lost: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
        if fault == "update_not_sent":
            monkeypatch.setattr(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantCommandNotSent("not sent")),
            )
        else:
            command = _UPDATE if fault == "update_dropped" else _GET
            lost = dropped_on(hass, command, await pooled_socket(core_client))

        with lost:
            result = await tools.ha_set_entity(entity_id=ids, **change)

        assert [row["entity_id"] for row in result["failed"]] == ids
        for row in result["failed"]:
            assert row["code"] == "CONNECTION_FAILED"
            assert row.get("write_outcome_unknown", False) is may_have_landed

    async def test_disabling_a_script_in_a_list_is_refused(
        self, tools: EntityTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id=["light.a", "SCRIPT.night"], enabled=False
            )

        # The blocked id, not the list's first, picks the message.
        assert "does not disable the script" in error_of(exc_info)["message"]
        sent.assert_not_called()

    async def test_a_compact_single_reply_leaves_out_the_entry(
        self, tools: EntityTools, entity_registry: er.EntityRegistry
    ) -> None:
        registry_entity(entity_registry, "light.a")

        result = await tools.ha_set_entity(
            entity_id="light.a", hidden=True, include_entity_entry=False
        )

        assert "entity_entry" not in result
        assert stored_entry(entity_registry, "light.a").hidden_by is not None

    async def test_an_unknown_label_is_refused_before_any_entity_is_touched(
        self,
        tools: EntityTools,
        entity_registry: er.EntityRegistry,
        labels: list[str],
        sent: AsyncMock,
    ) -> None:
        """One check at the entry guards the whole list."""
        for entity_id in ("light.a", "light.b"):
            registry_entity(entity_registry, entity_id)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_entity(
                entity_id=["light.a", "light.b"], labels=["ghost_label"]
            )

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert sent_types(sent) == [_LABELS]
