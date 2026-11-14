"""Routing of ``ha_config_list_helpers`` over the ``ha_mcp_tools`` component.

When the component advertises ``helpers_list``, one ``ha_mcp_tools/helpers_list``
command serves the listing and the ``{helper_type}/list`` read is skipped. Each
record keeps its storage ``id`` and gains the current ``entity_id`` and display
``name`` from the entity registry (#1794). A storage type falls back to
``{helper_type}/list``: silently when the component no longer knows the command
or did not enumerate the type, with a warning when its command fails, times out
or cannot be sent. A flow type (``template``, ...) and ``helper_type="all"`` have
no such list, so every component miss is a ``COMPONENT_NOT_INSTALLED`` error.

The server's own client runs on a real ``hass``. Helpers are created through
Core's own commands, flow helpers are ``MockConfigEntry`` rows, and Core answers
both the component's command and the legacy list. A spy on the pooled socket
records which of them the tool sent.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import websocket_api as component_ws_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import component_api, helper_flows, tools_config_helpers
from ha_mcp.tools.component_api import invalidate_caps

from ._client_spies import sent_commands
from ._core_helper_flows import CORE_HELPER_FLOWS
from ._routing_core import (
    held_command,
    list_helpers,
    pooled_socket,
    sent_frames,
    sent_types,
    setup_every_simple_type,
    setup_storage_booleans,
)
from ._tool_errors import error_body, error_of

HELPERS_LIST = "ha_mcp_tools/helpers_list"


class Renamed(NamedTuple):
    storage_id: str
    entity_id: str
    old_name: str
    new_name: str


@pytest.fixture
async def core(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's registry API and storage input_booleans, without the component."""
    await setup_storage_booleans(hass)


@pytest.fixture
async def client(
    core: None, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return component_core_client


async def _renamed_boolean(hass: HomeAssistant, client: HomeAssistantClient) -> Renamed:
    """An input_boolean created as one name whose entity was renamed since."""
    suffix = uuid.uuid4().hex[:8]
    old_name, new_name = f"Old Name {suffix}", f"New Name {suffix}"
    created = await client.send_websocket_message(
        {"type": "input_boolean/create", "name": old_name, "icon": "mdi:flash"}
    )
    storage_id = created["result"]["id"]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "input_boolean", "input_boolean", storage_id
    )
    assert entity_id is not None
    registry.async_update_entity(entity_id, name=new_name)
    await hass.async_block_till_done()
    return Renamed(storage_id, entity_id, old_name, new_name)


def _template_helper(hass: HomeAssistant) -> tuple[MockConfigEntry, str]:
    """A template sensor helper: its config entry and its entity's id."""
    entry = MockConfigEntry(
        domain="template", title="Templated", options={"state": "{{ 1 + 1 }}"}
    )
    entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "template", f"templated_{entry.entry_id}", config_entry=entry
    )
    er.async_get(hass).async_update_entity(entity.entity_id, name="Templated Sensor")
    return entry, entity.entity_id


async def _rejected(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await list_helpers(client, **kwargs)
    return error_body(exc)


def _legacy_warning(response: dict[str, Any]) -> bool:
    return any("served via legacy path" in w for w in response.get("warnings", []))


async def _register_failing_helpers_list(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The component with a ``helpers_list`` whose handler raises: Core answers
    the command with an error."""

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise HomeAssistantError("helpers_list failed")

    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api._do_helpers_list", fail
    )
    component_ws_api.async_register_commands(hass)


async def _register_helpers_list_without_coverage(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The component as an older build answered: no ``covered_types``."""

    def older(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"helpers": [], "count": 0}

    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api._do_helpers_list", older
    )
    component_ws_api.async_register_commands(hass)


# --- storage types served by the component -------------------------------------


async def test_the_component_serves_a_storage_type_and_is_probed_once(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    await _renamed_boolean(hass, client)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        first = await list_helpers(client, helper_type="input_boolean")
        second = await list_helpers(client, helper_type="input_boolean")

    assert (first["success"], first["helper_type"]) == (True, "input_boolean")
    assert first["count"] == second["count"] == 1
    types = sent_types(sent)
    assert "input_boolean/list" not in types
    assert types.count("ha_mcp_tools/info") == 1
    frames = sent_frames(sent, HELPERS_LIST)
    assert len(frames) == 2
    assert frames[0]["helper_types"] == ["input_boolean"]
    assert frames[0]["include_flow_helpers"] is False


async def test_a_renamed_helper_keeps_its_storage_id_and_shows_its_current_name(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    renamed = await _renamed_boolean(hass, client)

    resp = await list_helpers(client, helper_type="input_boolean")

    (record,) = resp["helpers"]
    assert record["id"] == renamed.storage_id
    assert record["icon"] == "mdi:flash"
    assert record["entity_id"] == renamed.entity_id
    assert record["name"] == renamed.new_name


async def test_the_component_and_the_legacy_list_return_the_same_envelope(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _renamed_boolean(hass, client)
    component = await list_helpers(client, helper_type="input_boolean")
    # Without the command Core answers unknown_command: the legacy list serves.
    monkeypatch.delitem(hass.data["websocket_api"], HELPERS_LIST)

    legacy = await list_helpers(client, helper_type="input_boolean")

    assert set(component) == set(legacy)
    assert component["success"] == legacy["success"] is True
    assert component["helper_type"] == legacy["helper_type"] == "input_boolean"
    assert component["count"] == legacy["count"] == 1
    assert "warnings" not in component
    assert "warnings" not in legacy


async def test_a_component_without_the_command_falls_back_silently(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component downgraded since the capability probe: Core answers
    unknown_command, the cached capabilities are dropped and the legacy list
    serves without a warning."""
    await _renamed_boolean(hass, client)
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], HELPERS_LIST)
    socket = await pooled_socket(client)

    with (
        sent_commands(socket) as sent,
        patch.object(
            tools_config_helpers, "invalidate_caps", wraps=invalidate_caps
        ) as invalidate,
    ):
        resp = await list_helpers(client, helper_type="input_boolean")

    assert resp["success"] is True
    assert resp["count"] == 1
    assert sent_types(sent).count("input_boolean/list") == 1
    assert not _legacy_warning(resp)
    invalidate.assert_called_once_with(client)


async def test_a_failing_component_command_falls_back_with_a_warning(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback record joins the registry like the inline legacy body
    (#1945): a renamed helper shows its current entity_id and name."""
    await _register_failing_helpers_list(hass, monkeypatch)
    renamed = await _renamed_boolean(hass, core_client)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        resp = await list_helpers(core_client, helper_type="input_boolean")

    assert resp["success"] is True
    assert sent_types(sent).count("input_boolean/list") == 1
    assert _legacy_warning(resp)
    (record,) = resp["helpers"]
    assert record["entity_id"] == renamed.entity_id
    assert record["name"] == renamed.new_name
    assert record["original_name"] == renamed.old_name


async def test_an_unreachable_component_socket_falls_back_with_a_warning(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The socket for the component read cannot be opened after the probe;
    the legacy list still answers, and joins the registry too (#1945)."""
    renamed = await _renamed_boolean(hass, client)
    assert await component_api.get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with (
        patch.object(
            tools_config_helpers,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        sent_commands(socket) as sent,
    ):
        resp = await list_helpers(client, helper_type="input_boolean")

    assert resp["success"] is True
    assert sent_types(sent).count("input_boolean/list") == 1
    assert _legacy_warning(resp)
    (record,) = resp["helpers"]
    assert (record["entity_id"], record["name"], record["original_name"]) == (
        renamed.entity_id,
        renamed.new_name,
        renamed.old_name,
    )


async def test_a_dead_socket_is_a_structured_connection_error(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component read and its legacy fallback both get no answer."""
    await _renamed_boolean(hass, client)
    assert await component_api.get_component_caps(client) is not None

    with (
        patch.object(
            tools_config_helpers,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        patch.object(
            client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
    ):
        body = await _rejected(client, helper_type="input_boolean")

    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert body["helper_type"] == "input_boolean"


async def test_a_timed_out_component_command_falls_back_with_a_warning(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    await _renamed_boolean(hass, client)
    socket = await pooled_socket(client)

    async with held_command(hass, client, HELPERS_LIST):
        with sent_commands(socket) as sent:
            resp = await list_helpers(client, helper_type="input_boolean")

    assert resp["success"] is True
    assert sent_types(sent).count("input_boolean/list") == 1
    assert _legacy_warning(resp)


async def test_the_person_fallback_flattens_storage_and_yaml_persons(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``person/list`` answers ``{"storage": [...], "config": [...]}``; the
    fallback lists both halves as records and paginates them."""
    assert await async_setup_component(
        hass, "person", {"person": [{"id": "bob", "name": "Bob"}]}
    )
    created = await core_client.send_websocket_message(
        {"type": "person/create", "name": "Alice"}
    )
    await _register_failing_helpers_list(hass, monkeypatch)

    resp = await list_helpers(core_client, helper_type="person")

    assert _legacy_warning(resp)
    assert sorted(h["id"] for h in resp["helpers"]) == sorted(
        [created["result"]["id"], "bob"]
    )
    assert resp["count"] == resp["total_count"] == 2
    assert resp["has_more"] is False
    assert resp["next_offset"] is None


async def test_without_the_component_the_legacy_list_serves(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    await _renamed_boolean(hass, core_client)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        resp = await list_helpers(core_client, helper_type="input_boolean")

    assert resp["success"] is True
    assert resp["count"] == 1
    types = sent_types(sent)
    assert types.count("input_boolean/list") == 1
    assert HELPERS_LIST not in types
    assert not _legacy_warning(resp)


async def test_a_flow_helper_never_shows_in_a_storage_type_listing(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    renamed = await _renamed_boolean(hass, client)
    _template_helper(hass)

    resp = await list_helpers(client, helper_type="input_boolean")

    (record,) = resp["helpers"]
    assert record["id"] == renamed.storage_id
    assert "entry_id" not in record
    assert "options" not in record


async def test_a_storage_type_the_component_did_not_enumerate_is_listed_by_core(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component cannot enumerate tags; its empty answer is not taken as
    "no tags", and the legacy list serves without a warning."""
    assert await async_setup_component(hass, "tag", {})
    await client.send_websocket_message({"type": "tag/create", "tag_id": "tag-42"})
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await list_helpers(client, helper_type="tag")

    types = sent_types(sent)
    assert types.count(HELPERS_LIST) == 1
    assert types.count("tag/list") == 1
    assert resp["helper_type"] == "tag"
    assert [h["id"] for h in resp["helpers"]] == ["tag-42"]
    assert not _legacy_warning(resp)


async def test_an_answer_without_covered_types_is_not_trusted_for_a_storage_type(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_helpers_list_without_coverage(hass, monkeypatch)
    renamed = await _renamed_boolean(hass, core_client)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        resp = await list_helpers(core_client, helper_type="input_boolean")

    types = sent_types(sent)
    assert types.count(HELPERS_LIST) == 1
    assert types.count("input_boolean/list") == 1
    assert [h["id"] for h in resp["helpers"]] == [renamed.storage_id]


async def test_a_renamed_zone_is_listed_by_its_storage_id(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component's object id follows the entity_id; the record's ``id``
    must stay the storage id an update needs."""
    assert await async_setup_component(hass, "zone", {})
    created = await client.send_websocket_message(
        {
            "type": "zone/create",
            "name": f"Office {uuid.uuid4().hex[:8]}",
            "latitude": 52.0,
            "longitude": 4.0,
            "radius": 100,
        }
    )
    storage_id = created["result"]["id"]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("zone", "zone", storage_id)
    assert entity_id is not None
    registry.async_update_entity(
        entity_id, new_entity_id=f"zone.workplace_{storage_id}"
    )
    await hass.async_block_till_done()

    resp = await list_helpers(client, helper_type="zone")

    (record,) = resp["helpers"]
    assert record["id"] == storage_id
    assert record["entity_id"] == f"zone.workplace_{storage_id}"


# --- flow types: the component or an error --------------------------------------


async def test_a_flow_type_is_served_by_the_component(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry, entity_id = _template_helper(hass)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await list_helpers(client, helper_type="template")

    assert (resp["success"], resp["helper_type"], resp["count"]) == (
        True,
        "template",
        1,
    )
    (record,) = resp["helpers"]
    assert record["helper_type"] == "template"
    assert record["entry_id"] == entry.entry_id
    assert record["entity_id"] == entity_id
    assert record["name"] == "Templated Sensor"
    assert record["options"] == {"state": "{{ 1 + 1 }}"}
    assert "id" not in record
    assert "template/list" not in sent_types(sent)
    (frame,) = sent_frames(sent, HELPERS_LIST)
    assert frame["helper_types"] == ["template"]
    assert frame["include_flow_helpers"] is True


async def test_a_flow_type_without_the_component_is_an_error(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    _template_helper(hass)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        body = await _rejected(core_client, helper_type="template")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert "flow-based" in body["error"]["message"]
    assert not {HELPERS_LIST, "template/list"} & set(sent_types(sent))


async def test_a_flow_type_whose_component_command_fails_is_an_error(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_failing_helpers_list(hass, monkeypatch)
    _template_helper(hass)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        body = await _rejected(core_client, helper_type="template")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert "template/list" not in sent_types(sent)


async def test_a_flow_type_on_a_component_without_the_command_is_an_error(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _template_helper(hass)
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], HELPERS_LIST)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        body = await _rejected(client, helper_type="template")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert "template/list" not in sent_types(sent)


async def test_a_flow_type_whose_component_command_times_out_is_an_error(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _template_helper(hass)
    socket = await pooled_socket(client)

    async with held_command(hass, client, HELPERS_LIST):
        with sent_commands(socket) as sent:
            body = await _rejected(client, helper_type="template")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert "template/list" not in sent_types(sent)


async def test_a_flow_type_answer_without_covered_types_is_an_error(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer that does not say it enumerated the type is not "no
    such helpers", and a flow type has no list to fall back to."""
    await _register_helpers_list_without_coverage(hass, monkeypatch)
    _template_helper(hass)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        body = await _rejected(core_client, helper_type="template")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert "template/list" not in sent_types(sent)


async def test_a_custom_helper_flow_left_out_by_a_failed_loader_read_names_it(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed component whose loader read failed is not told to update."""
    monkeypatch.setattr(
        "ha_mcp.tools.helper_flows._fetch_helper_flow_types",
        AsyncMock(return_value=CORE_HELPER_FLOWS | {"my_custom_helper"}),
    )
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api.flow_domains"
        ".async_get_config_flows",
        AsyncMock(side_effect=HomeAssistantError("loader failed")),
    )

    with pytest.raises(ToolError) as exc:
        await list_helpers(client, helper_type="my_custom_helper")

    error = error_of(exc)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "loader" in error["message"]
    assert "my_custom_helper" in error["message"]


# --- helper_type="all" -----------------------------------------------------------


async def test_all_types_merges_the_component_listing_with_persons_and_tags(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component enumerates storage and flow helpers; persons and tags,
    which it cannot enumerate, come from their own lists."""
    await setup_every_simple_type(hass)
    renamed = await _renamed_boolean(hass, client)
    entry, _ = _template_helper(hass)
    await client.send_websocket_message({"type": "person/create", "name": "Alice"})
    await client.send_websocket_message({"type": "tag/create", "tag_id": "tag-42"})
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await list_helpers(client, helper_type="all")

    assert (resp["success"], resp["helper_type"], resp["count"]) == (True, "all", 4)
    by_type = {h.get("helper_type"): h for h in resp["helpers"]}
    assert set(by_type) == {"input_boolean", "template", "person", "tag"}
    assert (by_type["input_boolean"]["id"], by_type["input_boolean"]["name"]) == (
        renamed.storage_id,
        renamed.new_name,
    )
    assert by_type["input_boolean"]["entity_id"] == renamed.entity_id
    assert by_type["template"]["entry_id"] == entry.entry_id
    assert by_type["template"]["options"] == {"state": "{{ 1 + 1 }}"}
    assert by_type["person"]["name"] == "Alice"
    assert by_type["tag"]["id"] == "tag-42"
    types = sent_types(sent)
    assert (types.count("person/list"), types.count("tag/list")) == (1, 1)
    (frame,) = sent_frames(sent, HELPERS_LIST)
    assert "helper_types" not in frame
    assert frame["include_flow_helpers"] is True


@pytest.mark.parametrize("helper_type", ["input_boolean", "all"])
async def test_a_yaml_helper_is_not_listed_as_a_stored_helper(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    helper_type: str,
) -> None:
    """``input_boolean/list`` lists stored helpers only; a YAML one must not
    appear as though its state attributes were its stored config."""
    client = component_core_client
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"yaml_flag": {"name": "YAML Flag"}}}
    )
    await setup_every_simple_type(hass)
    renamed = await _renamed_boolean(hass, client)
    assert hass.states.get("input_boolean.yaml_flag") is not None

    resp = await list_helpers(client, helper_type=helper_type)

    listed = {h.get("entity_id") for h in resp["helpers"]}
    assert renamed.entity_id in listed
    assert "input_boolean.yaml_flag" not in listed


async def test_all_types_refuses_a_listing_that_misses_a_custom_helper_flow(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core lists a helper flow the component did not cover: an error, not a
    partial listing."""
    await setup_every_simple_type(hass)
    monkeypatch.setattr(
        "ha_mcp.tools.helper_flows._fetch_helper_flow_types",
        AsyncMock(return_value=CORE_HELPER_FLOWS | {"my_custom_helper"}),
    )

    body = await _rejected(client, helper_type="all")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert "my_custom_helper" in body["error"]["message"]


async def test_all_types_reads_the_helper_flows_afresh(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A helper integration uninstalled since the cached read is not reported
    as missing from the component's listing."""
    await setup_every_simple_type(hass)
    monkeypatch.setattr(
        "ha_mcp.tools.helper_flows._fetch_helper_flow_types",
        AsyncMock(side_effect=[CORE_HELPER_FLOWS | {"gone_helper"}, CORE_HELPER_FLOWS]),
    )
    assert "gone_helper" in await helper_flows.helper_flow_types(client)

    resp = await list_helpers(client, helper_type="all")

    assert resp["success"] is True


async def test_all_types_with_an_unreadable_helper_flow_list_is_a_connection_error(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_every_simple_type(hass)
    monkeypatch.setattr(
        "ha_mcp.tools.helper_flows._fetch_helper_flow_types",
        AsyncMock(side_effect=HomeAssistantConnectionError("flow_handlers down")),
    )
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        body = await _rejected(client, helper_type="all")

    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert body["helper_type"] == "all"
    assert not [t for t in sent_types(sent) if t.endswith("/list")]


async def test_all_types_surfaces_a_failed_registry_join_as_a_warning(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A type served by its own list joins the registry (#1945); when that
    read fails (here Core has no registry API), the merged listing says so."""
    await setup_every_simple_type(hass)
    await component_core_client.send_websocket_message(
        {"type": "tag/create", "tag_id": "tag-42"}
    )

    resp = await list_helpers(component_core_client, helper_type="all")

    assert resp["success"] is True
    assert any("entity registry" in w for w in resp.get("warnings", []))


async def test_all_types_without_the_component_is_an_error(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    await setup_every_simple_type(hass)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        body = await _rejected(core_client, helper_type="all")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    types = sent_types(sent)
    assert HELPERS_LIST not in types
    assert not [t for t in types if t.endswith("/list")]


async def test_all_types_whose_component_command_fails_is_an_error(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup_every_simple_type(hass)
    await _register_failing_helpers_list(hass, monkeypatch)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        body = await _rejected(core_client, helper_type="all")

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert not [t for t in sent_types(sent) if t.endswith("/list")]
