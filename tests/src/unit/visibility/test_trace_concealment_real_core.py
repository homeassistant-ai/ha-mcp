"""Enforce mode conceals a hidden item's trace records, on real Core.

The trace tool and a raw ``trace/*`` command name a run by ``domain`` and
``item_id``, where ``item_id`` is the item's entity-registry ``unique_id``. The
hidden automations and scripts here are real registry rows with real states, and
the rows and details are Core's own trace objects stored through
``async_store_trace`` and read back over the server's client, so the aliases the
enforcement scans match come from Core's registry rather than a typed-in reply.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.visibility.model import VisibilityConfig

from .._tool_errors import error_of
from .._trace_fixtures import automation_run, script_run, store_runs
from ._chained_enforcement import _ChainedEnforcement, _returns, make_context

_HIDDEN_AUTOMATION = "automation.private"
_HIDDEN_AUTOMATION_KEY = "1700000000001"
_HIDDEN_SCRIPT = "script.front_door"
_HIDDEN_SCRIPT_KEY = "come_home"
_VISIBLE_AUTOMATION = "automation.visible"
_VISIBLE_AUTOMATION_KEY = "1700000000002"
_VISIBLE_SCRIPT = "script.front_door_2"
_VISIBLE_SCRIPT_KEY = "front_door"
# A hidden automation whose trace key is the object ID of a different, visible
# automation: the trace tool resolves the latter's own registry ``unique_id``.
_LOOKALIKE_HIDDEN_KEY = "morning"
_LOOKALIKE_VISIBLE = "automation.morning"
_LOOKALIKE_VISIBLE_KEY = "other"
# A visible config id that literally starts with its own domain, so a
# domain-prefix strip would make it collide with the hidden item's key.
_DOMAIN_PREFIXED_KEY = f"automation.{_HIDDEN_AUTOMATION_KEY}"


@pytest.fixture(autouse=True)
async def trace_api(hass: HomeAssistant) -> None:
    """Set up Core's trace store and the registry API the tools read."""
    assert await async_setup_component(hass, "trace", {})
    assert await async_setup_component(hass, "config", {})


def _register(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    unique_id: str,
) -> er.RegistryEntry:
    """Create the registry row Core creates for a loaded automation or script."""
    domain = entity_id.partition(".")[0]
    entry = entity_registry.async_get_or_create(
        domain,
        domain,
        unique_id,
        suggested_object_id=entity_id.partition(".")[2],
    )
    assert (entry.entity_id, entry.unique_id) == (entity_id, unique_id)
    hass.states.async_set(entity_id, "on")
    return entry


def _tool_result(payload: dict[str, Any]) -> SimpleNamespace:
    """Wrap a tool's real returned dict as the result the outbound scan reads."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload, default=str))],
        structured_content=payload,
    )


async def _unreached_call_next(_context: Any) -> Any:
    raise AssertionError("enforcement must refuse the call before the tool runs")


def _registry_reads(send: Any) -> int:
    """How many ``config/entity_registry/list`` reads the client spy sent."""
    return sum(
        1
        for call in send.await_args_list
        if isinstance(call.args[0], dict)
        and call.args[0].get("type") == "config/entity_registry/list"
    )


def _enforce(set_config: Callable[..., VisibilityConfig], *hidden: str) -> None:
    set_config(enabled=True, enforce=True, deny_entity_ids=list(hidden))


def _tool_calls(client: HomeAssistantClient, **args: Any) -> Any:
    """A ``call_next`` that runs the real trace tool with ``args``."""

    async def _call(_context: Any) -> Any:
        return _tool_result(await TraceTools(client).ha_get_automation_traces(**args))

    return _call


def _raw_trace_calls(client: HomeAssistantClient, message: dict[str, Any]) -> Any:
    """A ``call_next`` that returns the raw trace command reply Core sends."""

    async def _call(_context: Any) -> Any:
        return _tool_result(await client.send_websocket_message(message))

    return _call


class TestHiddenTraceTargets:
    """A hidden item's own trace key is concealed before Core is asked."""

    @pytest.mark.parametrize(
        ("entity_id", "unique_id", "hidden", "args"),
        [
            pytest.param(
                _HIDDEN_AUTOMATION,
                _HIDDEN_AUTOMATION_KEY,
                _HIDDEN_AUTOMATION,
                {"entity_id": _HIDDEN_AUTOMATION},
                id="hidden-entity-id",
            ),
            pytest.param(
                _HIDDEN_SCRIPT,
                _HIDDEN_SCRIPT_KEY,
                _HIDDEN_SCRIPT,
                # The script was renamed, so its storage key is not its object
                # id; the trace tool falls back to ``script.<storage key>``.
                {"entity_id": f"script.{_HIDDEN_SCRIPT_KEY}"},
                id="hidden-storage-key-as-prefixed-entity-id",
            ),
            pytest.param(
                _HIDDEN_AUTOMATION,
                _HIDDEN_AUTOMATION_KEY,
                _HIDDEN_AUTOMATION,
                {"domain": "automation", "item_id": _HIDDEN_AUTOMATION_KEY},
                id="hidden-domain-and-item-id",
            ),
        ],
    )
    async def test_a_hidden_trace_target_is_concealed(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        entity_id: str,
        unique_id: str,
        hidden: str,
        args: dict[str, str],
    ) -> None:
        """None of the target spellings reaches the tool or reveals the item."""
        _register(hass, entity_registry, entity_id, unique_id)
        _enforce(set_config, hidden)
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context("ha_get_automation_traces", args), _unreached_call_next
            )

        assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"


class TestRawTraceCommandTargets:
    """A raw trace command's target goes through the same alias check."""

    async def test_a_raw_trace_command_naming_a_hidden_storage_key_is_concealed(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The storage key in ``trace/get`` data is concealed before Core is asked."""
        _register(hass, entity_registry, _HIDDEN_SCRIPT, _HIDDEN_SCRIPT_KEY)
        _enforce(set_config, _HIDDEN_SCRIPT)
        mw = _ChainedEnforcement(lambda: core_client)
        context = make_context(
            "ha_call_service",
            {
                "ws_command": "trace/get",
                "data": {
                    "domain": "script",
                    "item_id": _HIDDEN_SCRIPT_KEY,
                    "run_id": "hidden-run",
                },
            },
        )

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(context, _unreached_call_next)

        assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"

    async def test_a_raw_trace_command_naming_a_visible_item_id_passes(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A visible trace key is not concealed by the hidden script's object id."""
        _register(hass, entity_registry, _HIDDEN_SCRIPT, _HIDDEN_SCRIPT_KEY)
        _register(hass, entity_registry, _VISIBLE_SCRIPT, _VISIBLE_SCRIPT_KEY)
        await store_runs(
            hass,
            script_run(
                "visible-run",
                context_id="own-visible",
                item_id=_VISIBLE_SCRIPT_KEY,
            ),
        )
        _enforce(set_config, _HIDDEN_SCRIPT)
        mw = _ChainedEnforcement(lambda: core_client)
        context = make_context(
            "ha_call_service",
            {
                "ws_command": "trace/get",
                "data": {
                    "domain": "script",
                    "item_id": _VISIBLE_SCRIPT_KEY,
                    "run_id": "visible-run",
                },
            },
        )

        result = await mw.on_call_tool(
            context,
            _raw_trace_calls(
                core_client,
                {
                    "type": "trace/get",
                    "domain": "script",
                    "item_id": _VISIBLE_SCRIPT_KEY,
                    "run_id": "visible-run",
                },
            ),
        )

        assert result.structured_content["success"] is True
        assert result.structured_content["result"]["run_id"] == "visible-run"


class TestHiddenTraceResults:
    """A trace result that names a hidden run is refused after the tool runs."""

    async def test_a_hidden_run_resolved_by_its_context_is_refused(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Core's own-context map resolves the run, then the scan refuses it."""
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        await store_runs(
            hass,
            automation_run(
                "hidden-run",
                context_id="own-hidden",
                item_id=_HIDDEN_AUTOMATION_KEY,
            ),
        )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context("ha_get_automation_traces", {"context_id": "own-hidden"}),
                _tool_calls(core_client, context_id="own-hidden"),
            )

        assert error_of(exc_info)["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_a_domain_list_naming_a_hidden_run_is_refused(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A domain-wide list must not leak a hidden item's row."""
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        await store_runs(
            hass,
            automation_run(
                "hidden-run", context_id="own-hidden", item_id=_HIDDEN_AUTOMATION_KEY
            ),
        )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context("ha_get_automation_traces", {"domain": "automation"}),
                _tool_calls(core_client, domain="automation"),
            )

        assert error_of(exc_info)["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_a_raw_trace_command_result_naming_a_hidden_run_is_refused(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A raw ``trace/list`` reply is scanned like the trace tool's result."""
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        await store_runs(
            hass,
            automation_run(
                "hidden-run", context_id="own-hidden", item_id=_HIDDEN_AUTOMATION_KEY
            ),
        )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)

        async def _raw_list(_context: Any) -> Any:
            return _tool_result(
                await core_client.send_websocket_message(
                    {"type": "trace/list", "domain": "automation"}
                )
            )

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context(
                    "ha_call_service",
                    {"ws_command": "trace/list", "data": {"domain": "automation"}},
                ),
                _raw_list,
            )

        assert error_of(exc_info)["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_a_nested_context_match_naming_a_hidden_run_is_refused(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        component_core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The scan reaches ``context_other_matches``, not only the selected run."""
        _register(hass, entity_registry, _VISIBLE_AUTOMATION, _VISIBLE_AUTOMATION_KEY)
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        with freeze_time("2026-11-11T12:00:00+00:00") as clock:
            await store_runs(
                hass,
                automation_run(
                    "visible-run",
                    context_id="own-visible",
                    parent_id="parent-1",
                    item_id=_VISIBLE_AUTOMATION_KEY,
                ),
            )
            clock.tick(timedelta(minutes=1))
            await store_runs(
                hass,
                automation_run(
                    "hidden-run",
                    context_id="own-hidden",
                    parent_id="parent-1",
                    item_id=_HIDDEN_AUTOMATION_KEY,
                ),
            )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: component_core_client)

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context("ha_get_automation_traces", {"context_id": "parent-1"}),
                _tool_calls(component_core_client, context_id="parent-1"),
            )

        assert error_of(exc_info)["code"] == "ENTITY_VISIBILITY_ENFORCED"


class TestVisibleTraceKeys:
    """A visible run's own key stays readable in enforce mode."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(
                {
                    "entity_id": _VISIBLE_AUTOMATION,
                    "run_id": "visible-run",
                },
                id="entity-id",
            ),
            pytest.param(
                {
                    "domain": "automation",
                    "item_id": _VISIBLE_AUTOMATION_KEY,
                    "run_id": "visible-run",
                },
                id="domain-and-item-id",
            ),
        ],
    )
    async def test_a_visible_run_stays_readable(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        args: dict[str, str],
    ) -> None:
        """Hiding another automation does not close the visible one's detail."""
        _register(hass, entity_registry, _VISIBLE_AUTOMATION, _VISIBLE_AUTOMATION_KEY)
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        await store_runs(
            hass,
            automation_run(
                "visible-run",
                context_id="own-visible",
                item_id=_VISIBLE_AUTOMATION_KEY,
            ),
        )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)

        result = await mw.on_call_tool(
            make_context("ha_get_automation_traces", args),
            _tool_calls(core_client, **args),
        )

        assert result.structured_content["run_id"] == "visible-run"
        assert result.structured_content["trace"]["action/0"]

    async def test_a_visible_trace_key_that_starts_with_its_domain_stays_readable(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A trace item_id is matched exactly, not after stripping the domain."""
        _register(hass, entity_registry, _VISIBLE_AUTOMATION, _DOMAIN_PREFIXED_KEY)
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        await store_runs(
            hass,
            automation_run(
                "visible-run",
                context_id="own-visible",
                item_id=_DOMAIN_PREFIXED_KEY,
            ),
        )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)
        args = {
            "domain": "automation",
            "item_id": _DOMAIN_PREFIXED_KEY,
            "run_id": "visible-run",
        }

        result = await mw.on_call_tool(
            make_context("ha_get_automation_traces", args),
            _tool_calls(core_client, **args),
        )

        assert result.structured_content["run_id"] == "visible-run"

    async def test_a_visible_trace_key_equal_to_a_hidden_object_id_stays_readable(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Traces key on the registry unique_id, not the hidden script's object id."""
        _register(hass, entity_registry, _HIDDEN_SCRIPT, _HIDDEN_SCRIPT_KEY)
        _register(hass, entity_registry, _VISIBLE_SCRIPT, _VISIBLE_SCRIPT_KEY)
        await store_runs(
            hass,
            script_run(
                "visible-run",
                context_id="own-visible",
                item_id=_VISIBLE_SCRIPT_KEY,
            ),
        )
        _enforce(set_config, _HIDDEN_SCRIPT)
        mw = _ChainedEnforcement(lambda: core_client)
        args = {
            "domain": "script",
            "item_id": _VISIBLE_SCRIPT_KEY,
            "run_id": "visible-run",
        }

        result = await mw.on_call_tool(
            make_context("ha_get_automation_traces", args),
            _tool_calls(core_client, **args),
        )

        assert result.structured_content["run_id"] == "visible-run"


class TestLookalikeObjectId:
    """A visible entity_id is checked by its own registry key, not its object ID."""

    async def test_a_visible_entity_id_is_not_concealed_by_a_lookalike_hidden_key(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The tool resolves the visible entity's own key, so the call proceeds."""
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _LOOKALIKE_HIDDEN_KEY)
        _register(hass, entity_registry, _LOOKALIKE_VISIBLE, _LOOKALIKE_VISIBLE_KEY)
        await store_runs(
            hass,
            automation_run(
                "visible-run",
                context_id="own-visible",
                item_id=_LOOKALIKE_VISIBLE_KEY,
            ),
        )
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)
        args = {"entity_id": _LOOKALIKE_VISIBLE, "run_id": "visible-run"}

        result = await mw.on_call_tool(
            make_context("ha_get_automation_traces", args),
            _tool_calls(core_client, **args),
        )

        assert result.structured_content["run_id"] == "visible-run"
        assert result.structured_content["trace"]["action/0"]

    async def test_the_hidden_entity_id_is_still_concealed(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The registry key the hidden entity resolves to is concealed."""
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _LOOKALIKE_HIDDEN_KEY)
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context(
                    "ha_get_automation_traces", {"entity_id": _HIDDEN_AUTOMATION}
                ),
                _unreached_call_next,
            )

        assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"

    async def test_an_unregistered_entity_id_falls_back_to_the_object_id(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """Without a registry row the tool would use the object ID, so it fails closed."""
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _LOOKALIKE_HIDDEN_KEY)
        hass.states.async_set(_LOOKALIKE_VISIBLE, "on")
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context(
                    "ha_get_automation_traces", {"entity_id": _LOOKALIKE_VISIBLE}
                ),
                _unreached_call_next,
            )

        assert error_of(exc_info)["code"] == "ENTITY_NOT_FOUND"


class TestTraceQueryRefreshesTheHiddenSet:
    """A trace query never trusts a hidden set read before the item existed."""

    @pytest.mark.parametrize(
        ("tool", "args", "raw_message"),
        [
            pytest.param(
                "ha_get_automation_traces",
                {"domain": "automation"},
                None,
                id="trace-tool",
            ),
            pytest.param(
                "ha_call_service",
                {"ws_command": "trace/list", "data": {"domain": "automation"}},
                {"type": "trace/list", "domain": "automation"},
                id="raw-trace-command",
            ),
        ],
    )
    async def test_a_trace_query_refreshes_a_stale_hidden_set(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
        raw_message: dict[str, Any] | None,
    ) -> None:
        """A registry row created after the cached read still conceals the run."""
        hass.states.async_set("sensor.visible", "on")
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)
        call_next = (
            _raw_trace_calls(core_client, raw_message)
            if raw_message is not None
            else _tool_calls(core_client, **args)
        )
        with patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as send:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.visible"}),
                _returns(_tool_result({"state": "on"})),
            )
            assert _registry_reads(send) == 1

            # The hidden automation registers after the non-trace call cached
            # the hidden set, and Core now stores a run under its registry key.
            _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
            await store_runs(
                hass,
                automation_run(
                    "hidden-run",
                    context_id="own-hidden",
                    item_id=_HIDDEN_AUTOMATION_KEY,
                ),
            )

            with pytest.raises(ToolError) as exc_info:
                await mw.on_call_tool(make_context(tool, args), call_next)

            assert _registry_reads(send) == 2

        assert error_of(exc_info)["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_a_trace_query_with_a_failed_refresh_fails_closed(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale last-known-good set is not safe for a trace query."""
        hass.states.async_set("sensor.visible", "on")
        _register(hass, entity_registry, _HIDDEN_AUTOMATION, _HIDDEN_AUTOMATION_KEY)
        _enforce(set_config, _HIDDEN_AUTOMATION)
        mw = _ChainedEnforcement(lambda: core_client)
        await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "sensor.visible"}),
            _returns(_tool_result({"state": "on"})),
        )
        monkeypatch.setattr(
            core_client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("no answer")),
        )

        with pytest.raises(ToolError) as exc_info:
            await mw.on_call_tool(
                make_context("ha_get_automation_traces", {"domain": "automation"}),
                _unreached_call_next,
            )

        assert error_of(exc_info)["code"] == "ENTITY_VISIBILITY_ENFORCED"
