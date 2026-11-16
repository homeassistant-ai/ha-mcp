"""The hidden set enforce mode works from: its cache, its fallbacks and its consumers.

The hidden set is computed from Core's registry, device registry and states,
read over the server's own client. A refresh that gets no answer is a client
whose Home Assistant refuses the connection, or a dropped device-registry
read; a refused registry read is Core's own reply.
"""

from __future__ import annotations

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
from ha_mcp.visibility.enforcement import active_hidden_regex, scrub_records
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import VISIBILITY_FILENAME
from ha_mcp.visibility.resolver import (
    VisibilityDataUnavailable,
    hidden_entity_ids,
    load_hidden_set,
)

from .._command_faults import dropped_on, pooled_socket
from .._component_frames import frames, pooled
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
    monkeypatch.setattr(enforcement, "_CACHE_TTL_SECONDS", 0.0)


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


class TestHiddenRegex:
    """The regex the scans and the collection scrubs match hidden ids with."""

    async def test_boundary_matching(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")

        regex = await active_hidden_regex(core_client)

        assert regex is not None
        # Not a longer sibling id, nor an id with a leading word character.
        assert regex.search("sensor.foo2") is None
        assert regex.search("my_sensor.foo") is None
        assert regex.search("binary_sensor.foo") is None
        # A bare reference and a dotted-context template reference.
        assert regex.search("sensor.foo") is not None
        assert regex.search("states.sensor.foo") is not None
        assert regex.search("{{ states('sensor.foo') }}") is not None

    async def test_nothing_hidden_has_no_regex(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """An active category filter that matches no entity hides nothing."""
        set_config(enabled=True, enforce=True, exclude_categories=["diagnostic"])

        assert await active_hidden_regex(core_client) is None


class TestScrubSeam:
    """Collection reads omit the records naming a hidden entity."""

    async def test_scrub_records_drops_embedded_hidden_id(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        enforce_hiding(set_config, "input_boolean.hidden_probe")
        regex = await active_hidden_regex(core_client)
        assert regex is not None
        records: list[dict[str, Any]] = [
            {"entity_id": "automation.morning", "config": {"trigger": "sun"}},
            {
                "entity_id": "automation.night",
                "config": {"action": {"entity_id": "input_boolean.hidden_probe"}},
            },
            {"friendly_name": "Hidden Probe", "id": "input_boolean.hidden_probe"},
        ]

        kept = scrub_records(records, regex)

        assert [r.get("entity_id", r.get("id")) for r in kept] == ["automation.morning"]

    async def test_scrub_records_boundary_no_false_positive(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")
        regex = await active_hidden_regex(core_client)
        assert regex is not None
        records: list[dict[str, Any]] = [
            {"entity_id": "sensor.foo2"},
            {"entity_id": "my_sensor.foo"},
        ]

        assert scrub_records(records, regex) == records

    async def test_active_hidden_regex_none_when_enforce_off(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Inactive enforce mode costs the collection reads no Core read."""
        set_config(enabled=True, enforce=False, deny_entity_ids=["sensor.hidden"])

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            regex = await active_hidden_regex(core_client)

        assert regex is None
        states.assert_not_awaited()

    async def test_active_hidden_regex_refreshes_with_caller_client(
        self,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A cold cache is refreshed over the caller's own live client."""
        enforce_hiding(set_config, "sensor.hidden")

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            regex = await active_hidden_regex(core_client)

        assert regex is not None
        assert states.await_count == 1

    async def test_active_hidden_regex_uses_shared_cache(
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

        regex = await active_hidden_regex(unreachable_client)

        assert regex is not None
        assert regex.search("states.sensor.hidden reference")
        assert not regex.search("sensor.hidden2")

    async def test_active_hidden_regex_fail_soft_on_data_error(
        self,
        unreachable_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The call gates own fail-closed; a best-effort scrub just skips."""
        enforce_hiding(set_config, "sensor.hidden")

        assert await active_hidden_regex(unreachable_client) is None


class TestComponentSearchScrub:
    """The component search hides entities itself, not the configs naming them."""

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
