"""The hidden set enforce mode works from: its cache, its fallbacks and its consumers.

The hidden set is computed from Core's registry, device registry and states,
read over the server's own client. A refresh that gets no answer is a client
whose Home Assistant refuses the connection, or a dropped device-registry
read; a refused registry read is Core's own reply.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility import enforcement, resolver
from ha_mcp.visibility.enforcement import active_hidden_matcher
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import VISIBILITY_FILENAME
from ha_mcp.visibility.resolver import (
    VisibilityDataUnavailable,
    hidden_entity_ids,
    load_hidden_set,
)

from .._command_faults import dropped_on, pooled_socket, registry_lookup_failing
from .._component_frames import frames, pooled
from .._deep_search_core import automation, load_automations
from .._search_routing import SEARCH, routes
from .._visibility_core import enforce_hiding, error_code, unreached_call_next
from ._chained_enforcement import (
    _ChainedEnforcement,
    _returns,
    make_context,
    text_result,
)


@pytest.fixture(autouse=True)
async def registry_api(hass: HomeAssistant) -> None:
    """``config`` answers the registry lists the hidden set is computed from."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def mw(core_client: HomeAssistantClient) -> _ChainedEnforcement:
    return _ChainedEnforcement(lambda: core_client)


@pytest.fixture
def no_cache_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call finds the cached hidden set already expired."""
    monkeypatch.setattr(enforcement, "CACHE_TTL_SECONDS", 0.0)


async def _overview(mw: _ChainedEnforcement) -> None:
    await mw.on_call_tool(
        make_context("ha_get_overview", {}), _returns(text_result("clean"))
    )


class TestCacheAndFallback:
    async def test_cache_reused_within_ttl(
        self,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A burst of calls reads Core's registry and states once."""
        enforce_hiding(set_config, "sensor.a")

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            for _ in range(3):
                await _overview(mw)

        assert states.await_count == 1

    async def test_cache_invalidated_on_config_change(
        self,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A config edit takes effect on the next call, not after the cache expires."""
        enforce_hiding(set_config, "sensor.a")

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            await _overview(mw)
            enforce_hiding(set_config, "sensor.b")
            await _overview(mw)

        assert states.await_count == 2

    async def test_cache_expires_after_ttl(
        self,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        no_cache_lifetime: None,
    ) -> None:
        """A registry-side change (an area or label move) is picked up in time."""
        enforce_hiding(set_config, "sensor.a")

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            await _overview(mw)
            first_call_reads = states.await_count
            await _overview(mw)

        assert states.await_count > first_call_reads

    async def test_last_known_good_used_when_refresh_fails(
        self,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        no_cache_lifetime: None,
    ) -> None:
        """A refresh that gets no answer keeps concealing with the last good set."""
        enforce_hiding(set_config, "sensor.hidden")
        await _overview(mw)

        with (
            patch.object(
                core_client,
                "get_states",
                AsyncMock(side_effect=HomeAssistantConnectionError("ha down")),
            ) as states,
            pytest.raises(ToolError) as exc,
        ):
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                unreached_call_next,
            )

        states.assert_awaited()
        assert error_code(exc) == "ENTITY_NOT_FOUND"

    async def test_fail_closed_with_no_data(
        self,
        unreachable_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """With no good refresh ever, no call runs."""
        enforce_hiding(set_config, "sensor.hidden")
        mw = _ChainedEnforcement(lambda: unreachable_client)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.anything"}),
                unreached_call_next,
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"

    async def test_last_known_good_not_reused_across_config_change(
        self,
        unreachable_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        mw: _ChainedEnforcement,
    ) -> None:
        """A set cached under one denylist is a different policy than another's."""
        enforce_hiding(set_config, "sensor.a")
        await _overview(mw)
        enforce_hiding(set_config, "sensor.b")
        down = _ChainedEnforcement(lambda: unreachable_client)

        with pytest.raises(ToolError) as exc:
            await down.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.b"}),
                unreached_call_next,
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


class TestDeviceRegistry:
    """An area exclude needs the device registry: devices carry most areas."""

    async def test_area_exclude_conceals_an_entity_through_its_devices_area(
        self,
        hass: HomeAssistant,
        area_registry: ar.AreaRegistry,
        device_registry: dr.DeviceRegistry,
        entity_registry: er.EntityRegistry,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        entry = MockConfigEntry(domain="test")
        entry.add_to_hass(hass)
        bedroom = area_registry.async_create("Bedroom")
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test", "bed")}
        )
        device_registry.async_update_device(device.id, area_id=bedroom.id)
        entity_registry.async_get_or_create(
            "sensor",
            "test",
            "bed_temp",
            suggested_object_id="bed_temp",
            config_entry=entry,
            device_id=device.id,
        )
        hass.states.async_set("sensor.bed_temp", "20")
        set_config(enabled=True, enforce=True, exclude_areas=[bedroom.id])

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.bed_temp"}),
                unreached_call_next,
            )
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("ran")),
        )

        assert error_code(exc) == "ENTITY_NOT_FOUND"
        assert result.content[0].text == "ran"

    async def test_unanswered_device_registry_read_fails_closed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Without the device list, an entity in the area could slip through."""
        set_config(enabled=True, enforce=True, exclude_areas=["bedroom"])
        socket = await pooled_socket(core_client)

        with (
            dropped_on(hass, "config/device_registry/list", socket),
            pytest.raises(ToolError) as exc,
        ):
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                unreached_call_next,
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"

    @pytest.mark.parametrize(
        "command", ["config/device_registry/list", "config/area_registry/list"]
    )
    async def test_refused_read_of_the_device_or_area_names_fails_closed(
        self,
        hass: HomeAssistant,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        command: str,
    ) -> None:
        """Without them a hidden entity's name cannot be told from its
        device's or its area's, whatever the filter hides by."""
        enforce_hiding(set_config, "sensor.foo")

        with registry_lookup_failing(hass, command), pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                unreached_call_next,
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


class TestStrictResolver:
    """Enforcement resolves the hidden set strictly: a degradation raises."""

    async def test_strict_does_not_raise_on_benign_unknown_category(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A typo'd category is noted, not treated as unusable data."""
        registry = await core_client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        config = VisibilityConfig(
            enabled=True, exclude_categories=["bogus"], deny_entity_ids=["sensor.a"]
        )

        hidden, warnings = hidden_entity_ids(registry, config, strict=True)

        assert hidden == {"sensor.a"}
        assert any("unknown exclude_categories" in w for w in warnings)

    async def test_load_hidden_set_strict_raises_on_config_load_failure(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / VISIBILITY_FILENAME).write_text("{ corrupt", encoding="utf-8")
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
        registry = await core_client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )

        with pytest.raises(VisibilityDataUnavailable):
            await load_hidden_set(registry, strict=True)


class TestHiddenMatcher:
    """What the output scans and the collection scrubs count as a hidden id."""

    @pytest.fixture
    async def matcher(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> Any:
        """``sensor.foo`` is hidden beside two entities whose ids contain it."""
        hass.states.async_set("sensor.foo2", "1")
        hass.states.async_set("binary_sensor.foo", "on")
        enforce_hiding(set_config, "sensor.foo")
        matcher = await active_hidden_matcher(core_client)
        assert matcher is not None
        return matcher

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("sensor.foo", id="bare"),
            pytest.param("states.sensor.foo", id="dotted-path"),
            pytest.param("{{ states('sensor.foo') }}", id="template"),
            # Run together with a state, as a template without a separator prints it.
            pytest.param("12sensor.foo", id="glued-before"),
            pytest.param("sensor.foo99", id="glued-after"),
            # A word is only followed a little past the longest entity id.
            pytest.param("x" * 2000 + "sensor.foo", id="glued-into-a-long-word"),
        ],
    )
    async def test_hidden_id_found_wherever_it_stands(
        self, matcher: Any, text: str
    ) -> None:
        assert matcher.embeds(text)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("sensor.foo2", id="longer-sibling"),
            pytest.param("binary_sensor.foo", id="other-domain"),
            # Serialized, the id follows a newline's or a quote's escape.
            pytest.param(
                json.dumps("12\nbinary_sensor.foo"), id="after-newline-escape"
            ),
            pytest.param(json.dumps("“binary_sensor.foo”"), id="after-unicode-escape"),
        ],
    )
    async def test_another_entitys_own_id_is_not_a_reference(
        self, matcher: Any, text: str
    ) -> None:
        """Hiding ``sensor.foo`` must not make its siblings unreadable."""
        assert not matcher.embeds(text)

    async def test_hidden_id_starting_inside_another_entitys_id_is_found(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """``light.kitchen`` starts inside ``sensor.light``, whose own word is
        the visible ``binary_sensor.light``."""
        hass.states.async_set("binary_sensor.light", "on")
        enforce_hiding(set_config, "sensor.light", "light.kitchen")

        matcher = await active_hidden_matcher(core_client)

        assert matcher is not None
        assert not matcher.embeds("binary_sensor.light")
        assert matcher.embeds("binary_sensor.light.kitchen")

    async def test_nothing_hidden_has_no_matcher(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """An active category filter that matches no entity hides nothing."""
        set_config(enabled=True, enforce=True, exclude_categories=["diagnostic"])

        assert await active_hidden_matcher(core_client) is None


class TestHiddenNames:
    """A hidden entity's friendly name counts in output like its id (#3156)."""

    @staticmethod
    async def _hiding(
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        entity_id: str,
        name: str,
    ) -> Any:
        hass.states.async_set(entity_id, "87", {"friendly_name": name})
        enforce_hiding(set_config, entity_id)
        matcher = await active_hidden_matcher(core_client)
        assert matcher is not None
        return matcher

    @pytest.fixture
    async def matcher(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> Any:
        """``sensor.foo`` is hidden and named "Front Door Lock Battery"."""
        return await self._hiding(
            hass, core_client, set_config, "sensor.foo", "Front Door Lock Battery"
        )

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Front Door Lock Battery | 87", id="as-named"),
            pytest.param("front door lock battery: 87", id="other-case"),
            pytest.param("battery of Front Door Lock Battery", id="at-the-end"),
            # A list of names and states, one entity per line, serialized.
            pytest.param(
                json.dumps("Hall | on\nFront Door Lock Battery | 87"),
                id="after-newline-escape",
            ),
        ],
    )
    async def test_hidden_name_found_as_a_phrase_of_its_own(
        self, matcher: Any, text: str
    ) -> None:
        assert matcher.embeds(text)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Front Door Lock Batteryless", id="runs-on"),
            pytest.param("Old2Front Door Lock Battery", id="runs-in"),
            pytest.param("Front Door Lock | 87", id="part-of-the-name"),
        ],
    )
    async def test_another_phrase_is_not_the_hidden_name(
        self, matcher: Any, text: str
    ) -> None:
        assert not matcher.embeds(text)

    async def test_hidden_name_found_beside_a_visible_name_that_contains_it(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A visible helper named after the hidden sensor does not switch the
        scan for the sensor's own name off."""
        hass.states.async_set(
            "sensor.foo_avg", "80", {"friendly_name": "Front Door Lock Battery avg"}
        )
        matcher = await self._hiding(
            hass, core_client, set_config, "sensor.foo", "Front Door Lock Battery"
        )

        assert not matcher.embeds("Front Door Lock Battery avg | 80")
        assert matcher.embeds(
            "Front Door Lock Battery avg | 80\nFront Door Lock Battery | 87"
        )

    @pytest.mark.parametrize("carrier", ["entity", "device", "area"])
    async def test_name_does_not_count_where_it_is_part_of_a_visible_name(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        carrier: str,
    ) -> None:
        """A lock named after its device must not close every listing that
        names the device, its area or its other entities."""
        if carrier == "entity":
            hass.states.async_set(
                "binary_sensor.front_door_contact",
                "on",
                {"friendly_name": "Front Door Contact"},
            )
        elif carrier == "device":
            entry = MockConfigEntry(domain="test")
            entry.add_to_hass(hass)
            dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={("test", "lock")},
                name="Front Door",
            )
        else:
            ar.async_get(hass).async_create("Front Door")

        matcher = await self._hiding(
            hass, core_client, set_config, "lock.front_door", "Front Door"
        )

        assert not matcher.embeds("Front Door Contact | on")

    async def test_shorter_hidden_name_found_where_a_longer_one_starts(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """ "Garage Side Door" is hidden too, but here only "Garage Side" stands."""
        hass.states.async_set("lock.side", "locked", {"friendly_name": "Garage Side"})
        hass.states.async_set(
            "lock.side_door", "locked", {"friendly_name": "Garage Side Door"}
        )
        enforce_hiding(set_config, "lock.side", "lock.side_door")
        matcher = await active_hidden_matcher(core_client)
        assert matcher is not None

        assert matcher.embeds("Garage Side Doorbell | Garage Side | locked")

    async def test_name_of_a_device_at_the_end_of_a_text_stays_exempt(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A longer hidden name must not be read into the text's last words."""
        entry = MockConfigEntry(domain="test")
        entry.add_to_hass(hass)
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("test", "lock")},
            name="Front Door",
        )
        hass.states.async_set("lock.front", "locked", {"friendly_name": "Front Door"})
        hass.states.async_set(
            "sensor.level", "80", {"friendly_name": "Kitchen Battery Level"}
        )
        enforce_hiding(set_config, "lock.front", "sensor.level")
        matcher = await active_hidden_matcher(core_client)
        assert matcher is not None

        assert not matcher.embeds("locked the Front Door")

    async def test_name_with_a_letter_whose_lowercase_is_longer_is_found(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Lowercasing "İ" gives two characters; the name is still found."""
        matcher = await self._hiding(
            hass, core_client, set_config, "lock.ic_kapi", "İç Kapı Kilidi"
        )

        assert matcher.embeds("Salon | on\nİç Kapı Kilidi | locked")

    async def test_name_of_a_single_word_is_not_scanned_for(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Hiding the zone named "Home" must not close every result that
        says "Home Assistant"."""
        matcher = await self._hiding(hass, core_client, set_config, "zone.home", "Home")

        assert not matcher.embeds("Home Assistant is restarting")

    @pytest.mark.parametrize("query", ["Überwachung Batterie", "überwachung batterie"])
    async def test_name_the_caller_typed_is_not_a_leak_when_repeated(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        query: str,
    ) -> None:
        """A search that names the entity gets its own query back, which a
        JSON result writes with the umlaut escaped, capital or small."""
        matcher = await self._hiding(
            hass, core_client, set_config, "sensor.foo", "Überwachung Batterie"
        )
        repeated = json.dumps({"query": query, "entities": []})

        assert matcher.embeds(repeated)
        assert not matcher.for_call({"query": query}).embeds(repeated)

    async def test_name_in_a_template_does_not_open_the_result_that_lists_it(
        self, matcher: Any
    ) -> None:
        """A template that mentions the name gets itself back, not the
        entity's name and state from the result beside it."""
        args = {
            "template": "{# Front Door Lock Battery #}\n"
            "{{ states.sensor | map(attribute='name') | list }}"
        }
        for_call = matcher.for_call(args)

        assert not for_call.embeds(json.dumps({**args, "result": "[]"}))
        assert for_call.embeds(
            json.dumps({**args, "result": "['Front Door Lock Battery']"})
        )


class TestScrubSeam:
    """Collection reads omit the records naming a hidden entity."""

    @staticmethod
    async def _search(client: HomeAssistantClient, query: str) -> dict[str, Any]:
        """``ha_search`` on the path without the component."""
        response: dict[str, Any] = await SearchTools(
            client, SmartSearchTools(client=client)
        ).ha_search(query=query)
        return response

    async def test_search_omits_a_config_naming_a_hidden_entity(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        assert await async_setup_component(
            hass, "input_boolean", {"input_boolean": {"hidden_probe": {}}}
        )
        toggle = {
            "action": "input_boolean.toggle",
            "target": {"entity_id": "input_boolean.hidden_probe"},
        }
        await load_automations(
            hass,
            [
                automation("am", "Probe morning"),
                automation("pm", "Probe night", actions=[toggle]),
            ],
        )
        enforce_hiding(set_config, "input_boolean.hidden_probe")

        response = await self._search(core_client, "probe")

        assert [hit["entity_id"] for hit in response["automations"]] == [
            "automation.probe_morning"
        ]
        assert "hidden_probe" not in json.dumps(response)

    async def test_search_lists_a_config_naming_an_entity_created_since_the_cached_read(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Its id is a longer word around a hidden id that the cached read
        does not know, so the entities are read again before the scrub."""
        enforce_hiding(set_config, "sensor.foo")
        assert await active_hidden_matcher(core_client) is not None
        hass.states.async_set("sensor.foo_average", "1")
        above = {"trigger": "numeric_state", "entity_id": "sensor.foo_average"}
        await load_automations(
            hass, [automation("avg", "Average watch", triggers=[{**above, "above": 5}])]
        )

        response = await self._search(core_client, "average watch")

        assert [hit["entity_id"] for hit in response["automations"]] == [
            "automation.average_watch"
        ]

    async def test_search_keeps_a_config_naming_entities_whose_ids_extend_a_hidden_id(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        for entity_id in ("sensor.foo", "sensor.foo2", "binary_sensor.foo"):
            hass.states.async_set(entity_id, "1")
        state = {"trigger": "state", "entity_id": ["sensor.foo2", "binary_sensor.foo"]}
        await load_automations(
            hass, [automation("ext", "Extended watch", triggers=[state])]
        )
        enforce_hiding(set_config, "sensor.foo")

        response = await self._search(core_client, "extended watch")

        assert [hit["entity_id"] for hit in response["automations"]] == [
            "automation.extended_watch"
        ]

    async def test_active_hidden_matcher_none_when_enforce_off(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Inactive enforce mode costs the collection reads no Core read."""
        set_config(enabled=True, enforce=False, deny_entity_ids=["sensor.hidden"])

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            matcher = await active_hidden_matcher(core_client)

        assert matcher is None
        states.assert_not_awaited()

    async def test_active_hidden_matcher_refreshes_with_caller_client(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A cold cache is refreshed over the caller's own live client."""
        enforce_hiding(set_config, "sensor.hidden")

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            matcher = await active_hidden_matcher(core_client)

        assert matcher is not None
        assert states.await_count == 1

    async def test_active_hidden_matcher_uses_shared_cache(
        self,
        core_client: HomeAssistantClient,
        unreachable_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The scrub reuses the set the middleware cached, even over a dead client.

        A scrub that reached a cold cache through a stale server instance's
        closed client used to skip the scrub entirely.
        """
        enforce_hiding(set_config, "sensor.hidden")
        await _overview(mw)

        matcher = await active_hidden_matcher(unreachable_client)

        assert matcher is not None
        assert matcher.embeds("states.sensor.hidden reference")

    async def test_active_hidden_matcher_fail_soft_on_data_error(
        self,
        unreachable_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The call gates own fail-closed; a best-effort scrub just skips."""
        enforce_hiding(set_config, "sensor.hidden")

        assert await active_hidden_matcher(unreachable_client) is None


class TestComponentSearchScrub:
    """The component search hides entities and, in enforce mode, the configs
    naming them itself; the server's scrub adjusts the totals for what it drops."""

    @pytest.fixture
    async def search(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> Any:
        """``ha_search`` for helpers, run on the component's search command."""
        assert await async_setup_component(
            hass,
            "input_boolean",
            {
                "input_boolean": {
                    "hidden_probe": {"name": "Hidden probe"},
                    "visible_probe": {"name": "Visible probe"},
                }
            },
        )
        tools = SearchTools(
            component_core_client, SmartSearchTools(client=component_core_client)
        )
        socket = await pooled(component_core_client)

        async def _search() -> dict[str, Any]:
            with routes(component_core_client, socket) as spies:
                response: dict[str, Any] = await tools.ha_search(
                    query="probe", search_types=["helper"]
                )
            assert len(frames(spies.component, SEARCH)) == 1
            return response

        return _search

    @pytest.mark.parametrize(
        ("enforce", "helpers"),
        [
            pytest.param(
                True, ["input_boolean.visible_probe"], id="enforce-omits-hidden"
            ),
            pytest.param(
                False,
                ["input_boolean.hidden_probe", "input_boolean.visible_probe"],
                id="enforce-off-keeps-all",
            ),
        ],
    )
    async def test_config_buckets_scrubbed_and_totals_adjusted(
        self,
        set_config: Callable[..., VisibilityConfig],
        search: Any,
        enforce: bool,
        helpers: list[str],
    ) -> None:
        set_config(
            enabled=True,
            enforce=enforce,
            deny_entity_ids=["input_boolean.hidden_probe"],
        )

        response = await search()

        assert [r["entity_id"] for r in response["helpers"]] == helpers
        assert response["config_total_matches"] == response["count"] == len(helpers)
