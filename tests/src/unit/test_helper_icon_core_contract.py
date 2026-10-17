"""Where a storage helper's icon and other registry fields go, and what is
reported when its entity is missing, against the answers Core sends.

The fakes and the autouse ``_no_component`` fixture are imported from
``test_zone_tools_core_contract.py``: every test runs through Core's WebSocket
commands unless it patches the component calls itself.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.config_helpers.create import _execute_create_simple_helper
from ha_mcp.tools.config_helpers.update import (
    _execute_update_simple_helper,
    _update_via_component,
)
from ha_mcp.tools.tools_zones import ZoneTools

from .test_zone_tools_core_contract import (
    _FETCH_SCHEMAS,
    HOME_STATE,
    OFFICE,
    REGISTRY_BLOCKED,
    _client,
    _no_component,  # noqa: F401 - pytest fixture
    _sent,
    registered,  # noqa: F401 - pytest fixture
    removed,  # noqa: F401 - pytest fixture
)

LAMP = {
    "entity_id": "input_boolean.lamp",
    "platform": "input_boolean",
    "unique_id": "lamp",
}
# Core's person and tag fields, as the component serves them: no icon among them.
_PERSON_SCHEMAS = {
    "person": {
        action: [{"name": f} for f in ("name", "user_id", "device_trackers", "picture")]
        for action in ("create", "update")
    }
}
_TAG_SCHEMAS = {
    "tag": {
        "create": [{"name": f} for f in ("tag_id", "name", "description")],
        "update": [{"name": f} for f in ("name", "description")],
    }
}


class TestCreatedIconStaysClearable:
    """A new zone's icon goes to the entity registry, where it stays clearable.

    Core's zone/update merges into the stored item and rejects an empty icon, so
    an icon stored in a zone can never be removed (#2643). Person and tag have no
    icon field in Core's schemas, so their icon goes to the registry too. A
    created person's, tag's or zone's entity is looked up in the registry.
    """

    async def test_zone_create_writes_the_icon_to_its_registry_entity(self) -> None:
        """The new zone "Home" is zone.home_2: the built-in home zone holds zone.home."""
        home = {**OFFICE, "id": "home", "name": "Home"}
        client = _client(
            {
                "zone/create": {"success": True, "result": home},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "zone.home_2",
                            "platform": "zone",
                            "unique_id": "home",
                        }
                    ],
                },
            },
            states={"zone.home": HOME_STATE},
        )

        result = await ZoneTools(client).ha_set_zone(
            name="Home", latitude=52.5, longitude=13.4, icon="mdi:house"
        )

        (create,) = _sent(client, "zone/create")
        assert "icon" not in create
        assert _sent(client, "config/entity_registry/update")[0]["entity_id"] == (
            "zone.home_2"
        )
        assert result["entity_id"] == "zone.home_2"

    async def test_an_icon_that_found_no_entity_is_a_warning(self) -> None:
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": []}}
        )

        result = await ZoneTools(client).ha_set_zone(
            name="Office", latitude=52.5, longitude=13.4, icon="mdi:briefcase"
        )

        assert _sent(client, "config/entity_registry/update") == []
        assert "were not applied" in result["warnings"][0]

    @pytest.mark.parametrize(
        "failure",
        [REGISTRY_BLOCKED, HomeAssistantConnectionError("403 socket closed")],
        ids=["failure reply", "dropped connection"],
    )
    async def test_a_failed_registry_read_after_create_is_a_warning(
        self, failure: Any
    ) -> None:
        """The helper exists: an error would make a retrying caller create it
        twice."""
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": failure,
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            result = await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        assert result["success"] is True
        assert "could not be read" in result["warnings"][0]
        assert "403" in result["warnings"][0]

    async def test_a_category_without_its_entity_is_named_as_not_applied(self) -> None:
        client = _client(
            {
                "person/create": {"success": True, "result": {"id": "anna"}},
                "config/entity_registry/list": {"success": True, "result": []},
            }
        )

        result = await _execute_create_simple_helper(
            client, "person", "Anna", None, None, None, "family", False, False, {}
        )  # fmt: skip

        assert "the category given were not applied" in result["warnings"][0]

    async def test_person_create_writes_the_icon_to_the_registry(self) -> None:
        client = _client(
            {
                "person/create": {"success": True, "result": {"id": "anna"}},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "person.anna",
                            "platform": "person",
                            "unique_id": "anna",
                        }
                    ],
                },
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=_PERSON_SCHEMAS)):
            await _execute_create_simple_helper(
                client, "person", "Anna", "mdi:account", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "person/create")
        assert "icon" not in create
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:account"
        )

    async def test_tag_create_writes_the_icon_to_the_registry(self) -> None:
        client = _client(
            {
                "tag/create": {"success": True, "result": {"id": "abc-1"}},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "tag.front_door",
                            "platform": "tag",
                            "unique_id": "abc-1",
                        }
                    ],
                },
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=_TAG_SCHEMAS)):
            await _execute_create_simple_helper(
                client, "tag", "Front door", "mdi:nfc", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "tag/create")
        assert "icon" not in create
        assert _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:nfc"

    async def test_person_update_leaves_the_icon_out_of_the_stored_item(self) -> None:
        person = {"id": "anna", "name": "Anna", "device_trackers": []}
        entry = {"entity_id": "person.anna", "platform": "person", "unique_id": "anna"}
        client = _client(
            {
                "person/list": {
                    "success": True,
                    "result": {"storage": [person], "config": []},
                },
                "person/update": {"success": True, "result": person},
                "config/entity_registry/get": {"success": True, "result": entry},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=_PERSON_SCHEMAS)):
            await _execute_update_simple_helper(
                client, "person", "person.anna", "person.anna", None, "mdi:account",
                None, None, None, False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "person/update")
        assert "icon" not in update
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:account"
        )

    async def test_component_person_create_sends_the_icon_as_a_registry_field(
        self,
    ) -> None:
        write = AsyncMock(return_value=None)
        with (
            patch("ha_mcp.tools.config_helpers.create.write_helper_item", write),
            patch(_FETCH_SCHEMAS, AsyncMock(return_value=_PERSON_SCHEMAS)),
        ):
            await _execute_create_simple_helper(
                _client(
                    {
                        "person/create": {"success": True, "result": {"id": "anna"}},
                        "config/entity_registry/list": {"success": True, "result": []},
                    }
                ), "person", "Anna", "mdi:account", None, None, None,
                False, False, {},
            )  # fmt: skip

        assert "icon" not in write.call_args.args[3]
        assert write.call_args.kwargs["registry"] == {"icon": "mdi:account"}

    @pytest.mark.parametrize(
        ("helper_type", "schemas", "item", "in_item"),
        [
            ("person", _PERSON_SCHEMAS, {"id": "anna", "name": "Anna"}, False),
            (
                "input_boolean",
                {"input_boolean": {"update": [{"name": "name"}, {"name": "icon"}]}},
                {"id": "anna", "name": "Anna"},
                True,
            ),
        ],
        ids=["person", "input_boolean"],
    )
    async def test_component_update_follows_the_schema_and_sets_the_registry(
        self, helper_type: str, schemas: Any, item: Any, in_item: bool
    ) -> None:
        """The registry always takes the icon; the item only where Core has one."""
        write = AsyncMock(return_value=None)
        read = AsyncMock(return_value={"item": item, "item_id": "anna"})
        with (
            patch("ha_mcp.tools.config_helpers.update.read_stored_helper_item", read),
            patch("ha_mcp.tools.config_helpers.update.write_helper_item", write),
            patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)),
        ):
            await _update_via_component(
                _client(), helper_type, f"{helper_type}.anna", f"{helper_type}.anna",
                None, "mdi:account", None, None, None, {},
            )  # fmt: skip

        assert ("icon" in write.call_args.args[3]) is in_item
        assert write.call_args.kwargs["registry"] == {"icon": "mdi:account"}

    async def test_an_icon_the_schema_declares_lands_in_item_and_registry(self) -> None:
        stored = {"id": "lamp", "name": "Lamp", "icon": "mdi:lamp"}
        entry = {
            "entity_id": "input_boolean.lamp",
            "platform": "input_boolean",
            "unique_id": "lamp",
        }
        client = _client(
            {
                "input_boolean/list": {"success": True, "result": [stored]},
                "input_boolean/update": {"success": True, "result": stored},
                "config/entity_registry/get": {"success": True, "result": entry},
            }
        )
        schemas = {"input_boolean": {"update": [{"name": "name"}, {"name": "icon"}]}}

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
            await _execute_update_simple_helper(
                client, "input_boolean", "input_boolean.lamp", "input_boolean.lamp",
                None, "mdi:lightbulb", None, None, None, False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "input_boolean/update")
        assert update["icon"] == "mdi:lightbulb"
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:lightbulb"
        )

    async def test_tag_update_without_its_entity_is_a_warning(self) -> None:
        tag = {"id": "abc-1", "name": "Front door"}
        client = _client(
            {
                "tag/list": {"success": True, "result": [tag]},
                "tag/update": {"success": True, "result": tag},
                "config/entity_registry/list": {"success": True, "result": []},
            }
        )

        result = await _execute_update_simple_helper(
            client, "tag", "abc-1", "abc-1", None, None,
            "hall", None, None, True, False, {},
        )  # fmt: skip

        assert "the area given were not applied" in result["warnings"][0]
        # A tag update is never waited on, so the missing entity is not why.
        assert "not waited on" not in result["warnings"][0]

    async def test_a_failed_registry_read_on_tag_update_is_a_warning(self) -> None:
        """Even with no registry field given, the missing entity_id is named."""
        tag = {"id": "abc-1", "name": "Front door"}
        client = _client(
            {
                "tag/list": {"success": True, "result": [tag]},
                "tag/update": {"success": True, "result": tag},
                "config/entity_registry/list": REGISTRY_BLOCKED,
            }
        )

        result = await _execute_update_simple_helper(
            client, "tag", "tag.abc-1", "abc-1", "Front", None,
            None, None, None, False, False, {},
        )  # fmt: skip

        assert "could not be read" in result["warnings"][0]

    async def test_other_helpers_keep_the_icon_in_their_stored_item(self) -> None:
        """Core's schema declares the icon: it stays in the stored item."""
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {"success": True, "result": [LAMP]},
            }
        )
        schemas = {"input_boolean": {"create": [{"name": "name"}, {"name": "icon"}]}}

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
            await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "input_boolean/create")
        assert create["icon"] == "mdi:lamp"
        assert _sent(client, "config/entity_registry/update") == []

    async def test_an_icon_without_a_known_field_goes_to_the_registry(self) -> None:
        """The create sends the icon where ``collection_icon`` routes it; its
        cases are pinned in test_helper_icon_route."""
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {"success": True, "result": [LAMP]},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "input_boolean/create")
        assert "icon" not in create
        assert _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:lamp"

    async def test_the_registry_icon_goes_to_the_new_helper_s_own_entity(self) -> None:
        """A renamed helper holds input_boolean.lamp, so Core names the new one
        input_boolean.lamp_2: <type>.<id> would give the icon to the other."""
        renamed = {**LAMP, "unique_id": "other"}
        created = {**LAMP, "entity_id": "input_boolean.lamp_2"}
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [renamed, created],
                },
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            result = await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "config/entity_registry/update")
        assert update["entity_id"] == "input_boolean.lamp_2"
        assert result["entity_id"] == "input_boolean.lamp_2"

    async def test_an_icon_for_a_helper_without_a_registry_entry_is_not_applied(
        self,
    ) -> None:
        """No entry proves which entity is the new helper's: nothing is written."""
        renamed = {**LAMP, "unique_id": "other"}
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {"success": True, "result": [renamed]},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            result = await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        assert _sent(client, "config/entity_registry/update") == []
        assert any("icon given were not applied" in w for w in result["warnings"])

    async def test_a_cleared_icon_also_leaves_the_stored_item(self) -> None:
        """Clearing only the registry would bring an older stored icon back."""
        stored = {"id": "lamp", "name": "Lamp", "icon": "mdi:lamp"}
        entry = {
            "entity_id": "input_boolean.lamp",
            "platform": "input_boolean",
            "unique_id": "lamp",
        }
        client = _client(
            {
                "input_boolean/list": {"success": True, "result": [stored]},
                "input_boolean/update": {"success": True, "result": stored},
                "config/entity_registry/get": {"success": True, "result": entry},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            await _execute_update_simple_helper(
                client, "input_boolean", "input_boolean.lamp", "input_boolean.lamp",
                None, "", None, None, None, False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "input_boolean/update")
        assert "icon" not in update
        assert _sent(client, "config/entity_registry/update")[0]["icon"] is None

    async def test_component_create_sends_the_icon_as_a_registry_field(self) -> None:
        write = AsyncMock(return_value=None)
        with patch("ha_mcp.tools.config_helpers.create.write_helper_item", write):
            await ZoneTools(_client()).ha_set_zone(
                name="Office", latitude=52.5, longitude=13.4, icon="mdi:briefcase"
            )

        payload = write.call_args.args[3]
        assert "icon" not in payload
        assert write.call_args.kwargs["registry"] == {"icon": "mdi:briefcase"}
