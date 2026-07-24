"""Unit tests for entity visibility enforce mode (visibility/enforcement.py, #2015).

Drives ``VisibilityEnforcementMiddleware.on_call_tool`` directly with a fake
context + fake call_next (mirrors tests/src/unit/test_read_only.py). The config
load and the resolver's ``load_hidden_set`` are both steered by patching the
resolver module's ``get_data_dir`` / ``load_visibility_config`` seam, so no file
I/O or Docker is needed.
"""

import json
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import create_entity_not_found_error
from ha_mcp.visibility import enforcement, resolver
from ha_mcp.visibility.enforcement import (
    VisibilityEnforcementMiddleware,
    _build_hidden_regex,
)
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.resolver import VisibilityDataUnavailable

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_context(name, arguments=None):
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=arguments or {})
    )


def text_result(text="ok", structured=None):
    """A ToolResult stand-in with a text content block + structured content."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        structured_content=structured,
    )


async def _unreached_call_next(context):
    raise AssertionError("call_next should not run for a blocked/concealed call")


def _returns(result):
    async def _call_next(context):
        return result

    return _call_next


class FakeClient:
    """States + registry seam double for the enforcement refresh path."""

    def __init__(self, *, registry=None, states=None, device=None, fail=False):
        self.registry = (
            registry if registry is not None else {"success": True, "result": []}
        )
        self.states = states if states is not None else []
        self.device = device if device is not None else {"success": True, "result": []}
        self.fail = fail
        self.get_states_calls = 0

    async def get_states(self):
        self.get_states_calls += 1
        if self.fail:
            raise ConnectionError("ha down")
        return self.states

    async def send_websocket_message(self, msg):
        if self.fail:
            raise ConnectionError("ha down")
        mtype = msg["type"]
        if mtype == "config/entity_registry/list":
            return self.registry
        if mtype == "config/device_registry/list":
            return self.device
        raise AssertionError(f"unexpected ws message: {mtype}")


@pytest.fixture
def set_config(monkeypatch, tmp_path):
    """Install the active VisibilityConfig for both the middleware's active-check
    load and the resolver's internal load; returns a setter to (re)seed it."""
    state = {"config": VisibilityConfig()}
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(resolver, "load_visibility_config", lambda _d: state["config"])

    def _set(**kwargs):
        kwargs.setdefault("exclude_categories", [])
        state["config"] = VisibilityConfig(**kwargs)
        return state["config"]

    return _set


def _error_body(exc: pytest.ExceptionInfo[ToolError]) -> dict:
    return json.loads(exc.value.args[0])


# ---------------------------------------------------------------------------
# Inactive passthrough
# ---------------------------------------------------------------------------


class TestInactivePassthrough:
    async def test_disabled_passes_through(self, set_config):
        set_config(enabled=False, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
            _returns(text_result("body")),
        )
        assert result.content[0].text == "body"
        assert client.get_states_calls == 0  # no registry fetch when inactive

    async def test_enforce_off_passes_through(self, set_config):
        set_config(enabled=True, enforce=False, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
            _returns(text_result("body")),
        )
        assert result.content[0].text == "body"
        assert client.get_states_calls == 0

    async def test_no_active_dimension_passes_through(self, set_config):
        # enabled + enforce but every hide dimension cleared -> hides nothing.
        set_config(enabled=True, enforce=True)
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "sensor.whatever"}),
            _returns(text_result("body")),
        )
        assert result.content[0].text == "body"
        assert client.get_states_calls == 0


# ---------------------------------------------------------------------------
# Inbound: concealment + refusal
# ---------------------------------------------------------------------------


class TestInboundScan:
    async def test_exact_match_concealed_as_not_found(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                _unreached_call_next,
            )
        # Matches the canonical not-found helper shape (existence concealment is
        # best-effort — individual tools decorate their genuine not-founds
        # differently, see _conceal_as_not_found).
        assert _error_body(exc) == create_entity_not_found_error("sensor.hidden")

    async def test_exact_match_on_service_call_write_is_concealed(self, set_config):
        # The inbound scan applies to EVERY tool, including a write/service call.
        set_config(enabled=True, enforce=True, deny_entity_ids=["lock.front"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_service",
                    {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
                ),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_id"] == "lock.front"

    async def test_embedded_match_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_eval_template", {"template": "{{ states('sensor.foo') }}"}
                ),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == "ha_eval_template"

    async def test_clean_args_pass_to_tool(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.visible"}),
            _returns(text_result("visible body")),
        )
        assert result.content[0].text == "visible body"

    async def test_exact_match_as_dict_key_concealed(self, set_config):
        # HA config payloads key maps by entity_id (a scene's ``entities``); a
        # hidden id positioned as a dict KEY must be caught like a value.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_config_set_scene",
                    {"config": {"entities": {"sensor.hidden": {"state": "on"}}}},
                ),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_id"] == "sensor.hidden"

    async def test_embedded_match_in_dict_key_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_config_set_automation",
                    {"config": {"states.sensor.foo above 5": "alias"}},
                ),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


# ---------------------------------------------------------------------------
# Config-load failure fails closed with a structured error
# ---------------------------------------------------------------------------


class TestConfigLoadFailure:
    @pytest.fixture
    def breakable_config(self, monkeypatch, tmp_path):
        """A config seam whose load can be flipped to raising mid-test."""
        state = {"config": VisibilityConfig(), "broken": False}
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        def _load(_d):
            if state["broken"]:
                raise ValueError("entity_visibility.json is not valid JSON")
            return state["config"]

        monkeypatch.setattr(resolver, "load_visibility_config", _load)
        return state

    async def test_corrupt_config_with_no_prior_load_refuses(self, breakable_config):
        # A corrupt entity_visibility.json must not crash the call with a raw
        # ValueError (whole-server availability regression) NOR silently disable
        # enforcement. With no last-known-good config it fails closed with the
        # structured enforce-mode error.
        breakable_config["broken"] = True
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "entity_visibility.json" in body["error"]["message"]

    async def test_corrupt_config_after_enforce_off_load_passes_through(
        self, breakable_config
    ):
        # Availability: a non-enforce install whose file corrupts mid-session
        # keeps working on the last-known-good (enforce-off) config.
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("first")),
        )
        assert result.content[0].text == "first"
        breakable_config["broken"] = True
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("second")),
        )
        assert result.content[0].text == "second"

    async def test_corrupt_config_after_enforce_on_load_stays_enforced(
        self, breakable_config
    ):
        # Boundary: an enforce install whose file corrupts mid-session keeps
        # concealing on the last-known-good (enforce-on) config.
        breakable_config["config"] = VisibilityConfig(
            enabled=True,
            enforce=True,
            exclude_categories=[],
            deny_entity_ids=["sensor.hidden"],
        )
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.visible"}),
            _returns(text_result("fine")),
        )
        assert result.content[0].text == "fine"
        breakable_config["broken"] = True
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Proxy envelope unwrap
# ---------------------------------------------------------------------------


class TestProxyUnwrap:
    async def test_json_string_arguments_inner_exact_id_concealed(self, set_config):
        # ha_call_read_tool with arguments as a JSON *string* (small-model shape):
        # the raw string only *embeds* the id, but unwrapping parses it so the
        # inner exact entity_id yields concealment, not the generic refusal.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool",
                    {
                        "name": "ha_get_state",
                        "arguments": '{"entity_id": "sensor.hidden"}',
                    },
                ),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_id"] == "sensor.hidden"


# ---------------------------------------------------------------------------
# Outbound scan
# ---------------------------------------------------------------------------


class TestOutboundScan:
    async def test_text_content_hit_refused_without_naming_entity(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_logs", {}),
                _returns(text_result("log entry references sensor.foo here")),
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        # The refusal must NOT confirm the id the caller may not have known.
        assert "sensor.foo" not in json.dumps(body)

    async def test_structured_content_hit_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_config_get_dashboard", {}),
                _returns(
                    text_result("ok", structured={"cards": [{"entity": "sensor.foo"}]})
                ),
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_clean_output_passes_through(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        result = await mw.on_call_tool(
            make_context("ha_get_logs", {}),
            _returns(text_result("nothing restricted here", structured={"ok": True})),
        )
        assert result.content[0].text == "nothing restricted here"

    async def test_clean_tool_error_passes_through_unmodified(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        original = json.dumps(
            {"error": {"code": "SERVICE_CALL_FAILED", "message": "x"}}
        )

        async def _raise_clean(context):
            raise ToolError(original)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context("ha_get_logs", {}), _raise_clean)
        assert exc.value.args[0] == original

    async def test_tool_error_naming_hidden_id_replaced(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())

        async def _raise_leaky(context):
            raise ToolError(
                json.dumps({"error": {"message": "reading sensor.foo failed"}})
            )

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(make_context("ha_get_logs", {}), _raise_leaky)
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "sensor.foo" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Boundary-aware matching
# ---------------------------------------------------------------------------


class TestBoundaryRegex:
    def test_boundary_matching(self):
        regex = _build_hidden_regex({"sensor.foo"})
        assert regex is not None
        # Must NOT match a longer sibling id or an id with a leading word char.
        assert regex.search("sensor.foo2") is None
        assert regex.search("my_sensor.foo") is None
        assert regex.search("binary_sensor.foo") is None
        # MUST match a bare reference and a dotted-context template reference.
        assert regex.search("sensor.foo") is not None
        assert regex.search("states.sensor.foo") is not None
        assert regex.search("{{ states('sensor.foo') }}") is not None

    def test_empty_hidden_set_has_no_regex(self):
        assert _build_hidden_regex(set()) is None


# ---------------------------------------------------------------------------
# Unscannable surfaces
# ---------------------------------------------------------------------------


class TestUnscannableSurfaces:
    async def test_custom_tool_code_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_manage_custom_tool", {"code": "print(1)"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_custom_tool_run_saved_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_manage_custom_tool", {"run_saved": "t"}),
                _unreached_call_next,
            )

    async def test_custom_tool_list_saved_allowed(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        result = await mw.on_call_tool(
            make_context("ha_manage_custom_tool", {"list_saved": True}),
            _returns(text_result("saved tools list")),
        )
        assert result.content[0].text == "saved tools list"

    async def test_screenshot_tool_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_dashboard_screenshot", {"dashboard": "x"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_dashboard_with_screenshot_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_config_get_dashboard", {"include_screenshot": True}),
                _unreached_call_next,
            )

    async def test_plain_dashboard_read_allowed_and_scanned(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = VisibilityEnforcementMiddleware(get_client=lambda: FakeClient())
        result = await mw.on_call_tool(
            make_context("ha_config_get_dashboard", {}),
            _returns(text_result("clean dashboard config")),
        )
        assert result.content[0].text == "clean dashboard config"


# ---------------------------------------------------------------------------
# TTL cache + config-change invalidation + fallbacks
# ---------------------------------------------------------------------------


class TestCacheAndFallback:
    async def test_cache_reused_within_ttl(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.a"])
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        for _ in range(3):
            await mw.on_call_tool(
                make_context("ha_get_overview", {}), _returns(text_result("clean"))
            )
        assert client.get_states_calls == 1  # one refresh, then cache hits

    async def test_cache_invalidated_on_config_change(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.a"])
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        assert client.get_states_calls == 1
        # A config edit (different dimension values) must refresh within one call.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.b"])
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        assert client.get_states_calls == 2

    async def test_cache_expires_after_ttl(self, set_config, monkeypatch):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.a"])
        clock = {"t": 1000.0}
        monkeypatch.setattr(enforcement.time, "monotonic", lambda: clock["t"])
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        assert client.get_states_calls == 1
        clock["t"] += enforcement._CACHE_TTL_SECONDS + 1
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        assert client.get_states_calls == 2

    async def test_last_known_good_used_when_refresh_fails(
        self, set_config, monkeypatch
    ):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        clock = {"t": 1000.0}
        monkeypatch.setattr(enforcement.time, "monotonic", lambda: clock["t"])
        client = FakeClient()
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        # Prime the cache with a good refresh.
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        # Break the client and expire the cache: the refresh now fails, but the
        # last-known-good set still conceals the hidden entity (not fail-closed).
        client.fail = True
        clock["t"] += enforcement._CACHE_TTL_SECONDS + 1
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_fail_closed_with_no_data(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient(fail=True)  # no good refresh ever
        mw = VisibilityEnforcementMiddleware(get_client=lambda: client)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.anything"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


# ---------------------------------------------------------------------------
# Strict resolver mode (used only by enforcement)
# ---------------------------------------------------------------------------


class TestStrictResolver:
    def test_strict_raises_on_degraded_registry(self):
        cfg = VisibilityConfig(
            enabled=True, exclude_categories=[], deny_entity_ids=["s.a"]
        )
        with pytest.raises(VisibilityDataUnavailable):
            resolver.hidden_entity_ids({"success": False}, cfg, strict=True)

    def test_nonstrict_degrades_open_on_bad_registry(self):
        cfg = VisibilityConfig(
            enabled=True, exclude_categories=[], deny_entity_ids=["s.a"]
        )
        hidden, warnings = resolver.hidden_entity_ids({"success": False}, cfg)
        assert hidden == {"s.a"}  # deny still honored (fail-open)
        assert warnings

    def test_strict_does_not_raise_on_benign_unknown_category(self):
        cfg = VisibilityConfig(
            enabled=True, exclude_categories=["bogus"], deny_entity_ids=["s.a"]
        )
        reg = {"success": True, "result": []}
        hidden, warnings = resolver.hidden_entity_ids(reg, cfg, strict=True)
        assert hidden == {"s.a"}
        assert any("unknown exclude_categories" in w for w in warnings)

    async def test_load_hidden_set_strict_raises_on_config_load_failure(
        self, monkeypatch, tmp_path
    ):
        def _boom(_data_dir):
            raise ValueError("corrupt config")

        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(resolver, "load_visibility_config", _boom)
        with pytest.raises(VisibilityDataUnavailable):
            await resolver.load_hidden_set({"success": True, "result": []}, strict=True)

    async def test_load_hidden_set_nonstrict_fails_open_on_config_load_failure(
        self, monkeypatch, tmp_path
    ):
        def _boom(_data_dir):
            raise ValueError("corrupt config")

        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(resolver, "load_visibility_config", _boom)
        hidden, warnings = await resolver.load_hidden_set(
            {"success": True, "result": []}
        )
        assert hidden == set()
        assert warnings
