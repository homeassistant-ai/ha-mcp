"""Enforce mode's argument and result scans, proxy unwrapping and unscannable tools.

The middleware pair runs chained as the server registers it, on the server's
own client connected to a real ``hass``: the hidden set it scans for comes
from Core's registry and states. The tool behind it is a stand-in for the
tool's own output, which is what the scans read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import create_entity_not_found_error
from ha_mcp.visibility.model import VisibilityConfig

from .._tool_errors import error_body
from .._visibility_core import enforce_hiding, error_code, unreached_call_next
from ._chained_enforcement import (
    _ChainedEnforcement,
    _returns,
    make_context,
    text_result,
)


@pytest.fixture(autouse=True)
async def registry_api(hass: HomeAssistant) -> None:
    """``config`` answers the registry list the hidden set is computed from."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def mw(core_client: HomeAssistantClient) -> _ChainedEnforcement:
    return _ChainedEnforcement(lambda: core_client)


class TestInboundScan:
    async def test_exact_match_concealed_as_not_found(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        """The answer is the canonical not-found, as for an entity that is absent."""
        enforce_hiding(set_config, "sensor.hidden")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                unreached_call_next,
            )

        assert error_body(exc) == create_entity_not_found_error("sensor.hidden")

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(
                "ha_call_service",
                {"domain": "lock", "service": "unlock", "entity_id": "sensor.hidden"},
                id="service-call-write",
            ),
            # Config payloads key maps by entity_id (a scene's ``entities``).
            pytest.param(
                "ha_config_set_scene",
                {"config": {"entities": {"sensor.hidden": {"state": "on"}}}},
                id="dict-key",
            ),
        ],
    )
    async def test_exact_match_anywhere_in_the_arguments_is_concealed(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
    ) -> None:
        enforce_hiding(set_config, "sensor.hidden")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), unreached_call_next)

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_id"] == "sensor.hidden"

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(
                "ha_eval_template",
                {"template": "{{ states('sensor.foo') }}"},
                id="template",
            ),
            pytest.param(
                "ha_config_set_automation",
                {"config": {"states.sensor.foo above 5": "alias"}},
                id="dict-key",
            ),
            # A config sent as a JSON string: the id follows a newline's escape.
            pytest.param(
                "ha_config_set_automation",
                {"config": json.dumps({"description": "watch\nsensor.foo"})},
                id="json-string-after-newline",
            ),
        ],
    )
    async def test_embedded_match_refused(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), unreached_call_next)

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == tool

    async def test_clean_args_pass_to_tool(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        enforce_hiding(set_config, "sensor.hidden")

        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.visible"}),
            _returns(text_result("visible body")),
        )

        assert result.content[0].text == "visible body"

    async def test_longer_id_the_caller_typed_gets_the_tools_own_answer(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        """Refusing it, or the error echoing it, would tell the caller that a
        hidden id is part of what it typed."""
        enforce_hiding(set_config, "sensor.foo")

        async def _not_found(_context: Any) -> Any:
            raise ToolError(json.dumps(create_entity_not_found_error("sensor.foo2")))

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.foo2"}),
                _not_found,
            )

        assert error_body(exc) == create_entity_not_found_error("sensor.foo2")


class TestProxyUnwrap:
    async def test_json_string_arguments_inner_exact_id_concealed(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        """Arguments sent as a JSON string are parsed, so the exact id conceals.

        The raw string only embeds the id, which alone would give the generic
        refusal instead.
        """
        enforce_hiding(set_config, "sensor.hidden")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool",
                    {
                        "name": "ha_get_state",
                        "arguments": '{"entity_id": "sensor.hidden"}',
                    },
                ),
                unreached_call_next,
            )

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_id"] == "sensor.hidden"

    async def test_non_proxy_name_key_does_not_borrow_an_exemption(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        """Only call proxies dispatch; another tool's name key is just data."""
        enforce_hiding(set_config, "sensor.hidden")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_config_get_label",
                    {"label_id": "x", "name": "ha_get_skill_guide"},
                ),
                _returns(text_result("label mentions sensor.hidden")),
            )

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == "ha_config_get_label"

    async def test_retired_inner_name_is_reported_as_current_name(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        enforce_hiding(set_config, "sensor.hidden")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool",
                    {"name": "ha_get_addon", "arguments": {"slug": "core_ssh"}},
                ),
                _returns(text_result("result mentions sensor.hidden")),
            )

        body = error_body(exc)
        assert body["tool_name"] == "ha_get_app"
        assert "ha_get_app" in body["error"]["message"]

    async def test_proxy_fail_closed_error_names_inner_tool(
        self,
        unreachable_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        enforce_hiding(set_config, "sensor.hidden")
        mw = _ChainedEnforcement(lambda: unreachable_client)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool", {"name": "ha_get_logs", "arguments": {}}
                ),
                unreached_call_next,
            )

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == "ha_get_logs"

    async def test_proxy_outbound_error_names_inner_tool(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        enforce_hiding(set_config, "sensor.hidden")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool", {"name": "ha_get_logs", "arguments": {}}
                ),
                _returns(text_result("result mentions sensor.hidden")),
            )

        body = error_body(exc)
        assert body["tool_name"] == "ha_get_logs"
        assert "ha_get_logs" in body["error"]["message"]


class TestOutboundScan:
    async def test_text_content_hit_refused_without_naming_entity(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        """The refusal must not confirm an id the caller may not have known."""
        enforce_hiding(set_config, "sensor.foo")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_logs", {}),
                _returns(text_result("log entry references sensor.foo here")),
            )

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "sensor.foo" not in json.dumps(body)

    async def test_structured_content_hit_refused(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_config_get_dashboard", {}),
                _returns(
                    text_result("ok", structured={"cards": [{"entity": "sensor.foo"}]})
                ),
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"

    @pytest.mark.parametrize(
        "guidance",
        [
            pytest.param(
                {"skill_content": {"references/x.md": "entity_id: cover.garage_door"}},
                id="bundled-guide",
            ),
            pytest.param(
                {"best_practice_warnings": ["e.g. entity_id: cover.garage_door"]},
                id="checker-warnings",
            ),
        ],
    )
    async def test_best_practice_text_on_a_gated_write_result_is_not_scanned(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        guidance: dict[str, Any],
    ) -> None:
        """Its examples name entities that are not this install's, and the
        write has already happened when its result is scanned."""
        enforce_hiding(set_config, "cover.garage_door")
        payload = {"success": True, "entity_id": "script.night", **guidance}

        result = await mw.on_call_tool(
            make_context("ha_config_set_script", {"script_id": "night"}),
            _returns(text_result(json.dumps(payload), structured=payload)),
        )

        assert result.structured_content == payload

    @pytest.mark.parametrize(
        ("tool", "payload"),
        [
            pytest.param(
                "ha_eval_template",
                {"skill_content": {"x": "cover.garage_door"}},
                id="a-tool-that-attaches-no-guide",
            ),
            pytest.param(
                "ha_config_set_script",
                {"entity_id": "cover.garage_door", "skill_content": {"x": "text"}},
                id="beside-the-guide",
            ),
        ],
    )
    async def test_hidden_id_outside_a_write_tools_guide_is_still_refused(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        payload: dict[str, Any],
    ) -> None:
        enforce_hiding(set_config, "cover.garage_door")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(tool, {}),
                _returns(text_result(json.dumps(payload), structured=payload)),
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"

    async def test_hidden_id_on_a_later_line_of_a_list_refused(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        """A template listing one entity per line returned every hidden entity
        but the first: serialized, the newline before the id ends in a letter
        (#3156)."""
        enforce_hiding(set_config, "sensor.foo")
        payload = {"result": "sensor.visible | 12\nsensor.foo | 99"}

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_eval_template", {}),
                _returns(text_result(json.dumps(payload), structured=payload)),
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"

    async def test_entity_created_since_the_cached_read_is_not_a_hidden_id(
        self,
        hass: HomeAssistant,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """A helper named after a hidden entity must not have its own creation
        refused: its new id is a longer word the cached read does not know."""
        enforce_hiding(set_config, "sensor.foo")
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result())
        )
        hass.states.async_set("sensor.foo_average", "1")

        result = await mw.on_call_tool(
            make_context("ha_config_set_helper", {"name": "Foo average"}),
            _returns(text_result("created sensor.foo_average")),
        )

        assert result.content[0].text == "created sensor.foo_average"

    async def test_clean_output_passes_through(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")

        result = await mw.on_call_tool(
            make_context("ha_get_logs", {}),
            _returns(text_result("nothing restricted here", structured={"ok": True})),
        )

        assert result.content[0].text == "nothing restricted here"

    async def test_clean_tool_error_passes_through_unmodified(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")
        original = json.dumps(
            {"error": {"code": "SERVICE_CALL_FAILED", "message": "x"}}
        )

        async def _raise_clean(_context: Any) -> Any:
            raise ToolError(original)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context("ha_get_logs", {}), _raise_clean)

        assert exc.value.args[0] == original

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param("reading sensor.foo failed", id="plain"),
            # An error's JSON escapes the quote, and the escape ends in a letter.
            pytest.param("invalid input “sensor.foo”", id="typographic-quotes"),
        ],
    )
    async def test_tool_error_naming_hidden_id_replaced(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        message: str,
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")

        async def _raise_leaky(_context: Any) -> Any:
            raise ToolError(json.dumps({"error": {"message": message}}))

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context("ha_get_logs", {}), _raise_leaky)

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "sensor.foo" not in json.dumps(body)


class TestUnscannableSurfaces:
    @pytest.mark.parametrize(
        ("tool", "args", "refused_as"),
        [
            pytest.param(
                "ha_get_dashboard_screenshot",
                {"dashboard": "x"},
                "ha_get_dashboard_screenshot",
                id="screenshot-tool",
            ),
            pytest.param(
                "ha_config_get_dashboard",
                {"include_screenshot": True},
                "ha_config_get_dashboard",
                id="dashboard-read-with-screenshot",
            ),
            # The dashboard write tool hands back pixels with return_screenshot.
            pytest.param(
                "ha_config_set_dashboard",
                {"url_path": "clean-dash", "return_screenshot": True},
                "ha_config_set_dashboard",
                id="dashboard-write-with-screenshot",
            ),
            pytest.param(
                "ha_manage_custom_tool",
                {"code": "print(1)"},
                "ha_manage_custom_tool",
                id="sandbox-code",
            ),
            pytest.param(
                "ha_manage_custom_tool",
                {"run_saved": "t"},
                "ha_manage_custom_tool",
                id="sandbox-run-saved",
            ),
            pytest.param(
                "ha_call_read_tool",
                {
                    "name": "ha_config_get_dashboard",
                    "arguments": {"include_screenshot": True},
                },
                "ha_config_get_dashboard",
                id="proxied-dashboard-read-with-screenshot",
            ),
            pytest.param(
                "ha_call_write_tool",
                {
                    "name": "ha_config_set_dashboard",
                    "arguments": {"return_screenshot": True},
                },
                "ha_config_set_dashboard",
                id="proxied-dashboard-write-with-screenshot",
            ),
            pytest.param(
                "ha_call_write_tool",
                {"name": "ha_manage_custom_tool", "arguments": {"code": "print(1)"}},
                "ha_manage_custom_tool",
                id="proxied-sandbox-code",
            ),
            pytest.param(
                "ha_call_write_tool",
                {"name": "ha_manage_custom_tool", "arguments": {"run_saved": "t"}},
                "ha_manage_custom_tool",
                id="proxied-sandbox-run-saved",
            ),
            pytest.param(
                "ha_call_read_tool",
                {
                    "name": "ha_config_get_dashboard",
                    "arguments": json.dumps({"include_screenshot": True}),
                },
                "ha_config_get_dashboard",
                id="proxied-json-string-arguments",
            ),
        ],
    )
    async def test_unscannable_call_refused_before_dispatch(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
        refused_as: str,
    ) -> None:
        """Pixels and sandbox code can read any state no text scan can see."""
        enforce_hiding(set_config, "sensor.foo")

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context(tool, args), unreached_call_next)

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == refused_as

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(
                "ha_manage_custom_tool", {"list_saved": True}, id="list-saved-tools"
            ),
            pytest.param(
                "ha_config_set_dashboard",
                {"url_path": "clean-dash"},
                id="dashboard-write-without-screenshot",
            ),
            pytest.param("ha_config_get_dashboard", {}, id="plain-dashboard-read"),
        ],
    )
    async def test_scannable_call_on_the_same_tool_runs(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        tool: str,
        args: dict[str, Any],
    ) -> None:
        enforce_hiding(set_config, "sensor.foo")

        result = await mw.on_call_tool(
            make_context(tool, args), _returns(text_result("clean output"))
        )

        assert result.content[0].text == "clean output"
