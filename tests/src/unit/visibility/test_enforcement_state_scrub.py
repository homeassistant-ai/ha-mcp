"""Enforce mode omits hidden references from a visible entity's ``ha_get_state``.

A state read of a visible entity whose content names a hidden one gets that
content omitted with a warning, instead of being refused whole; a result the
warning cannot be attached to is refused. The hidden set comes from Core's
registry and states over the server's own client. Where the shape is one
``ha_get_state`` returns, the real tool reads Core's state; the other shapes
are tool results the middleware must still handle safely.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult
from ha_mcp._vendor.mcp.types import TextContent
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility.model import VisibilityConfig

from .._visibility_core import error_code
from ._chained_enforcement import _ChainedEnforcement, _returns, make_context

_ENFORCEMENT_LOGGER = "ha_mcp.visibility.enforcement"
_TRACKERS = ["device_tracker.hidden_a", "device_tracker.hidden_b"]
_SCRUB_PREFIX = "Entity Visibility enforce mode omitted "


@pytest.fixture(autouse=True)
async def registry_api(hass: HomeAssistant) -> None:
    """``config`` answers the registry list the hidden set is computed from."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def mw(core_client: HomeAssistantClient) -> _ChainedEnforcement:
    return _ChainedEnforcement(lambda: core_client)


@pytest.fixture
def only_person_visible(
    hass: HomeAssistant, set_config: Callable[..., VisibilityConfig]
) -> None:
    """A person whose trackers are hidden by an allowlist naming only the person."""
    for tracker in _TRACKERS:
        hass.states.async_set(tracker, "home")
    set_config(enabled=True, enforce=True, allow_entity_ids=["person.allowed"])


def _tool_result(payload: Any, text_payload: Any = None) -> ToolResult:
    """What FastMCP makes of a tool's return value: JSON text plus structured."""
    text = json.dumps(payload if text_payload is None else text_payload)
    return ToolResult(
        content=[TextContent(type="text", text=text)], structured_content=payload
    )


def _ha_get_state(client: HomeAssistantClient) -> Any:
    """The real ``ha_get_state``, run with the call's arguments."""
    tools = SearchTools(client, SmartSearchTools(client=client))

    async def _call_next(context: Any) -> ToolResult:
        return _tool_result(await tools.ha_get_state(**context.message.arguments))

    return _call_next


def _both_representations(result: Any) -> list[Any]:
    return [result.structured_content, json.loads(result.content[0].text)]


def _omitted(rendered: dict[str, Any]) -> str:
    """The paths the scrub's warning names: it comes last, after the tool's own."""
    warnings = rendered["warnings"]
    assert [w for w in warnings if w.startswith(_SCRUB_PREFIX)] == warnings[-1:]
    paths: str = warnings[-1].removeprefix(_SCRUB_PREFIX).split(" because ")[0]
    return paths


async def _get_state(mw: _ChainedEnforcement, call_next: Any, **arguments: Any) -> Any:
    return await mw.on_call_tool(make_context("ha_get_state", arguments), call_next)


class TestRealStateRead:
    async def test_attributes_naming_hidden_trackers_are_omitted_with_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        only_person_visible: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The whole attributes mapping goes: its values derive from the trackers."""
        hass.states.async_set(
            "person.allowed",
            "home",
            {
                "source": "device_tracker.hidden_a",
                "device_tracker.hidden_b": "mapping key is hidden too",
                "device_trackers": _TRACKERS,
                "friendly_name": "Allowed person",
            },
        )

        with caplog.at_level("INFO", logger=_ENFORCEMENT_LOGGER):
            result = await _get_state(
                mw, _ha_get_state(core_client), entity_id="person.allowed"
            )

        for rendered in _both_representations(result):
            assert "attributes" not in rendered["data"]
            assert rendered["data"]["state"] == "home"
            assert _omitted(rendered) == "data.attributes"
            assert "Entity Visibility section" in rendered["warnings"][-1]
            assert "device_tracker.hidden" not in json.dumps(rendered)
        scrub_logs = [
            r
            for r in caplog.records
            if r.msg.startswith("visibility enforce: scrubbed")
        ]
        # One omitted path per representation, both representations changed.
        assert [r.args for r in scrub_logs] == [(2, 2)]

    async def test_bulk_read_omits_hidden_content(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        only_person_visible: None,
    ) -> None:
        hass.states.async_set(
            "person.allowed",
            "home",
            {"source": "device_tracker.hidden_a", "friendly_name": "Allowed person"},
        )

        result = await _get_state(
            mw, _ha_get_state(core_client), entity_id=["person.allowed"]
        )

        for rendered in _both_representations(result):
            record = rendered["data"]["states"]["person.allowed"]
            assert "attributes" not in record
            assert record["state"] == "home"
            assert _omitted(rendered) == "data.states.person.allowed.attributes"

    async def test_scrub_warning_is_appended_to_the_tools_own_warnings(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        only_person_visible: None,
    ) -> None:
        """A state that is a hidden entity_id goes; the tool's warning stays."""
        hass.states.async_set("person.allowed", "device_tracker.hidden_a")

        result = await _get_state(
            mw,
            _ha_get_state(core_client),
            entity_id="person.allowed",
            fields=["entity_id", "state"],
            attribute_keys=["friendly_name"],
        )

        for rendered in _both_representations(result):
            assert rendered["data"] == {"entity_id": "person.allowed"}
            assert _omitted(rendered) == "data.state"
            assert any("attribute_keys" in w for w in rendered["warnings"][:-1])


class TestOtherResultShapes:
    async def test_nested_record_naming_hidden_entity_is_dropped_whole(
        self, hass: HomeAssistant, mw: _ChainedEnforcement, only_person_visible: None
    ) -> None:
        """Its sibling fields (a friendly_name) would leak the hidden entity."""
        hass.states.async_set(
            "person.allowed", "home", {"friendly_name": "Allowed person"}
        )
        hass.states.async_set(
            "device_tracker.hidden_a", "home", {"friendly_name": "Secret"}
        )
        person, tracker = (
            hass.states.get(entity_id)
            for entity_id in ("person.allowed", "device_tracker.hidden_a")
        )
        assert person is not None and tracker is not None
        # A record carrying a related entity's state, as Core reports both.
        payload = {
            "data": {
                **{key: person.as_dict()[key] for key in ("entity_id", "state")},
                "attributes": dict(person.attributes),
                "related": [
                    {
                        **{
                            key: tracker.as_dict()[key]
                            for key in ("entity_id", "state")
                        },
                        **tracker.attributes,
                    }
                ],
            }
        }

        result = await _get_state(
            mw, _returns(_tool_result(payload)), entity_id="person.allowed"
        )

        for rendered in _both_representations(result):
            assert rendered["data"]["related"] == []
            assert rendered["data"]["attributes"] == {"friendly_name": "Allowed person"}
            assert "Secret" not in json.dumps(rendered)
            assert _omitted(rendered) == "data.related[0]"

    async def test_hidden_mapping_key_is_not_named_in_the_warning(
        self, mw: _ChainedEnforcement, only_person_visible: None
    ) -> None:
        payload = {
            "data": {
                "entity_id": "person.allowed",
                "state": "home",
                "device_tracker.hidden_a": "seen",
            }
        }

        result = await _get_state(
            mw, _returns(_tool_result(payload)), entity_id="person.allowed"
        )

        for rendered in _both_representations(result):
            assert rendered["data"] == {"entity_id": "person.allowed", "state": "home"}
            assert _omitted(rendered) == "data.<hidden key>"
            assert "device_tracker.hidden" not in json.dumps(rendered)

    @pytest.mark.parametrize(
        "result",
        [
            # Scrubbing only the dict half would ship a cleaned structured payload
            # next to an unscrubbed text block.
            pytest.param(
                _tool_result(
                    {"data": {"attributes": {"source": "device_tracker.hidden_a"}}},
                    text_payload=[
                        {"attributes": {"source": "device_tracker.hidden_a"}}
                    ],
                ),
                id="list-text-beside-dict-structured",
            ),
            pytest.param(
                _tool_result(
                    {
                        "data": {"attributes": {"source": "device_tracker.hidden_a"}},
                        "warnings": "not a list",
                    }
                ),
                id="warnings-not-a-list",
            ),
            pytest.param(
                ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                [{"attributes": {"source": "device_tracker.hidden_a"}}]
                            ),
                        )
                    ]
                ),
                id="list-root",
            ),
            pytest.param(
                ToolResult(
                    content=[
                        TextContent(
                            type="text", text="relationship: device_tracker.hidden_a"
                        )
                    ]
                ),
                id="not-json",
            ),
        ],
    )
    async def test_result_the_warning_cannot_be_attached_to_is_refused(
        self, mw: _ChainedEnforcement, only_person_visible: None, result: ToolResult
    ) -> None:
        with pytest.raises(ToolError) as exc:
            await _get_state(mw, _returns(result), entity_id="person.allowed")

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"
