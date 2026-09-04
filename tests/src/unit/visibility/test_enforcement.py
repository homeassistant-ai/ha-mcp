"""Unit tests for entity visibility enforce mode (visibility/enforcement.py, #2015).

Drives the inbound + outbound enforcement middleware pair directly with a fake
context + fake call_next (mirrors tests/src/unit/test_read_only.py), chained in
server registration order via ``make_mw``. The config load and the resolver's
``load_hidden_set`` are both steered by patching the resolver module's
``get_data_dir`` / ``load_visibility_config`` seam, so no file I/O or Docker is
needed.
"""

import json
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from ha_mcp.errors import create_entity_not_found_error
from ha_mcp.visibility import enforcement, resolver
from ha_mcp.visibility.enforcement import (
    VisibilityInboundEnforcement,
    VisibilityOutboundEnforcement,
    _build_hidden_regex,
    active_hidden_regex,
    scrub_records,
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


def report_context(*, proxied=False):
    arguments = {"fields": ["recent_logs"]}
    if proxied:
        return make_context(
            "ha_call_read_tool", {"name": "ha_report_issue", "arguments": arguments}
        )
    return make_context("ha_report_issue", arguments)


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


class _ChainedEnforcement:
    """Inbound + outbound halves chained in server registration order.

    The server registers the inbound half before read-only/policy and the
    outbound half innermost; from the enforcement pair's own perspective the
    composition is inbound-wrapping-outbound, which this reproduces so tests
    exercise the full conceal/refuse/scan pipeline through one entry point.
    """

    def __init__(self, get_client):
        self.inbound = VisibilityInboundEnforcement(get_client=get_client)
        self.outbound = VisibilityOutboundEnforcement(get_client=get_client)

    async def on_call_tool(self, context, call_next):
        async def _inner(ctx):
            return await self.outbound.on_call_tool(ctx, call_next)

        return await self.inbound.on_call_tool(context, _inner)


def make_mw(*, get_client):
    return _ChainedEnforcement(get_client)


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


@pytest.fixture(autouse=True)
def _fresh_hidden_cache(monkeypatch):
    """Isolate the module-shared hidden-set cache per test.

    The cache is deliberately module-global (a process can hold several server
    instances — see _HiddenSetCache), so without this every test would inherit
    the previous test's cached hidden set.
    """
    monkeypatch.setattr(enforcement, "_hidden_cache", enforcement._HiddenSetCache())


def _error_body(exc: pytest.ExceptionInfo[ToolError]) -> dict:
    return json.loads(exc.value.args[0])


# ---------------------------------------------------------------------------
# Inactive passthrough
# ---------------------------------------------------------------------------


class TestInactivePassthrough:
    async def test_disabled_passes_through(self, set_config):
        set_config(enabled=False, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
            _returns(text_result("body")),
        )
        assert result.content[0].text == "body"
        assert client.get_states_calls == 0  # no registry fetch when inactive

    async def test_enforce_off_passes_through(self, set_config):
        set_config(enabled=True, enforce=False, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)
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
        mw = make_mw(get_client=lambda: client)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "sensor.whatever"}),
            _returns(text_result("body")),
        )
        assert result.content[0].text == "body"
        assert client.get_states_calls == 0


class TestReportIssueRestriction:
    """The diagnostics tool stays reachable unless explicitly restricted."""

    @pytest.mark.parametrize("proxied", [False, True])
    async def test_report_issue_bypasses_active_enforcement_by_default(
        self, set_config, proxied
    ):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)

        result = await mw.on_call_tool(
            report_context(proxied=proxied),
            _returns(text_result("diagnostics mention sensor.hidden")),
        )

        assert result.content[0].text == "diagnostics mention sensor.hidden"
        assert client.get_states_calls == 0

    async def test_report_issue_bypasses_unavailable_visibility_data_by_default(
        self, set_config
    ):
        """The diagnostic escape hatch must not touch unavailable HA inputs."""
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient(fail=True)
        mw = make_mw(get_client=lambda: client)

        result = await mw.on_call_tool(
            report_context(),
            _returns(text_result("diagnostics remain reachable")),
        )

        assert result.content[0].text == "diagnostics remain reachable"
        assert client.get_states_calls == 0

    async def test_report_issue_opt_in_is_inert_while_enforce_is_off(self, set_config):
        set_config(
            enabled=True,
            enforce=False,
            deny_entity_ids=["sensor.hidden"],
            restrict_report_issue=True,
        )
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)

        result = await mw.on_call_tool(
            report_context(),
            _returns(text_result("diagnostics mention sensor.hidden")),
        )

        assert result.content[0].text == "diagnostics mention sensor.hidden"
        assert client.get_states_calls == 0

    async def test_default_bypass_is_logged(self, set_config, caplog):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)

        with caplog.at_level("INFO", logger="ha_mcp.visibility.enforcement"):
            await mw.on_call_tool(
                report_context(),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        bypass_messages = [
            record.message
            for record in caplog.records
            if "restrict_report_issue=false" in record.message
        ]
        assert bypass_messages == [
            "visibility enforce: bypassing ha_report_issue because "
            "restrict_report_issue=false"
        ]

    async def test_default_bypass_is_not_logged_when_enforcement_inactive(
        self, set_config, caplog
    ):
        set_config(enabled=True, enforce=False, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)

        with caplog.at_level("INFO", logger="ha_mcp.visibility.enforcement"):
            result = await mw.on_call_tool(
                report_context(),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        assert result.content[0].text == "diagnostics mention sensor.hidden"
        assert not any(
            "bypassing ha_report_issue" in record.message for record in caplog.records
        ), [record.message for record in caplog.records]

    async def test_default_bypass_is_not_logged_without_active_hide_dimension(
        self, set_config, caplog
    ):
        set_config(enabled=True, enforce=True)
        mw = make_mw(get_client=FakeClient)

        with caplog.at_level("INFO", logger="ha_mcp.visibility.enforcement"):
            result = await mw.on_call_tool(
                report_context(),
                _returns(text_result("diagnostics remain reachable")),
            )

        assert result.content[0].text == "diagnostics remain reachable"
        assert not any(
            "bypassing ha_report_issue" in record.message for record in caplog.records
        ), [record.message for record in caplog.records]

    @pytest.mark.parametrize("proxied", [False, True])
    async def test_report_issue_is_scanned_when_operator_opts_in(
        self, set_config, proxied
    ):
        set_config(
            enabled=True,
            enforce=True,
            deny_entity_ids=["sensor.hidden"],
            restrict_report_issue=True,
        )
        mw = make_mw(get_client=FakeClient)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                report_context(proxied=proxied),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


# ---------------------------------------------------------------------------
# Inbound: concealment + refusal
# ---------------------------------------------------------------------------


class TestInboundScan:
    async def test_exact_match_concealed_as_not_found(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.visible"}),
            _returns(text_result("visible body")),
        )
        assert result.content[0].text == "visible body"

    async def test_exact_match_as_dict_key_concealed(self, set_config):
        # HA config payloads key maps by entity_id (a scene's ``entities``); a
        # hidden id positioned as a dict KEY must be caught like a value.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _unreached_call_next,
            )
        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "entity_visibility.json" in body["error"]["message"]

    async def test_corrupt_config_with_no_prior_load_keeps_report_issue_available(
        self, breakable_config, caplog
    ):
        breakable_config["broken"] = True
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)

        with caplog.at_level("WARNING", logger="ha_mcp.visibility.enforcement"):
            result = await mw.on_call_tool(
                make_context("ha_report_issue", {"fields": ["recent_logs"]}),
                _returns(text_result("diagnostics still available")),
            )

        assert result.content[0].text == "diagnostics still available"
        assert client.get_states_calls == 0
        messages = [record.message for record in caplog.records]
        assert [
            message for message in messages if "allowing ha_report_issue" in message
        ] == [
            "visibility enforce: config load failed; allowing ha_report_issue because "
            "its diagnostic exemption defaults to unrestricted"
        ]
        assert not any("failing closed" in message for message in messages), messages

    async def test_corrupt_config_after_enforce_off_load_passes_through(
        self, breakable_config, caplog
    ):
        # Availability: a non-enforce install whose file corrupts mid-session
        # keeps working on the last-known-good (enforce-off) config.
        mw = make_mw(get_client=FakeClient)
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("first")),
        )
        assert result.content[0].text == "first"
        breakable_config["broken"] = True
        caplog.clear()
        with caplog.at_level("WARNING", logger="ha_mcp.visibility.enforcement"):
            result = await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _returns(text_result("second")),
            )
        assert result.content[0].text == "second"
        warnings = [
            record
            for record in caplog.records
            if "config load failed" in record.message
        ]
        assert [record.message for record in warnings] == [
            "visibility enforce: config load failed; using last-known-good config"
        ]
        assert warnings[0].exc_info is not None

    async def test_outbound_only_config_load_failure_logs(
        self, breakable_config, monkeypatch, caplog
    ):
        mw = make_mw(get_client=FakeClient)
        await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("prime")),
        )
        calls = 0

        def _load(_d):
            nonlocal calls
            calls += 1
            if calls == 1:
                return breakable_config["config"]
            raise ValueError("entity_visibility.json changed during the call")

        monkeypatch.setattr(resolver, "load_visibility_config", _load)
        caplog.clear()
        with caplog.at_level("WARNING", logger="ha_mcp.visibility.enforcement"):
            result = await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _returns(text_result("second")),
            )

        assert result.content[0].text == "second"
        warnings = [
            record
            for record in caplog.records
            if "config load failed" in record.message
        ]
        assert [record.message for record in warnings] == [
            "visibility enforce: config load failed; using last-known-good config"
        ]
        assert warnings[0].exc_info is not None

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
        mw = make_mw(get_client=FakeClient)
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

    async def test_corrupt_config_after_report_opt_in_stays_enforced(
        self, breakable_config
    ):
        breakable_config["config"] = VisibilityConfig(
            enabled=True,
            enforce=True,
            exclude_categories=[],
            deny_entity_ids=["sensor.hidden"],
            restrict_report_issue=True,
        )
        mw = make_mw(get_client=FakeClient)
        result = await mw.on_call_tool(
            make_context("ha_report_issue", {"fields": ["recent_logs"]}),
            _returns(text_result("clean diagnostics")),
        )
        assert result.content[0].text == "clean diagnostics"

        breakable_config["broken"] = True
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_report_issue", {"fields": ["recent_logs"]}),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


# ---------------------------------------------------------------------------
# Proxy envelope unwrap
# ---------------------------------------------------------------------------


class TestProxyUnwrap:
    async def test_json_string_arguments_inner_exact_id_concealed(self, set_config):
        # ha_call_read_tool with arguments as a JSON *string* (small-model shape):
        # the raw string only *embeds* the id, but unwrapping parses it so the
        # inner exact entity_id yields concealment, not the generic refusal.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)
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

    async def test_search_tool_extra_name_is_not_treated_as_dispatch(self, set_config):
        """ha_search_tools searches a catalog; it never executes its name key."""
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_search_tools",
                    {"query": "lights", "name": "ha_report_issue"},
                ),
                _returns(text_result("catalog mentions sensor.hidden")),
            )

        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == "ha_search_tools"

    async def test_retired_inner_name_is_reported_as_current_name(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool",
                    {"name": "ha_get_addon", "arguments": {"slug": "core_ssh"}},
                ),
                _returns(text_result("result mentions sensor.hidden")),
            )

        body = _error_body(exc)
        assert body["tool_name"] == "ha_get_app"
        assert "ha_get_app" in body["error"]["message"]

    async def test_proxy_fail_closed_error_names_inner_tool(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=lambda: FakeClient(fail=True))

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool",
                    {"name": "ha_get_logs", "arguments": {}},
                ),
                _unreached_call_next,
            )

        body = _error_body(exc)
        assert body["tool_name"] == "ha_get_logs"

    async def test_proxy_outbound_error_names_inner_tool(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = make_mw(get_client=FakeClient)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_call_read_tool",
                    {"name": "ha_get_logs", "arguments": {}},
                ),
                _returns(text_result("result mentions sensor.hidden")),
            )

        body = _error_body(exc)
        assert body["tool_name"] == "ha_get_logs"
        assert "ha_get_logs" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Outbound scan
# ---------------------------------------------------------------------------


def _hidden_tracker_client() -> FakeClient:
    """A client whose registry marks the ``device_tracker.hidden_*`` ids diagnostic.

    Paired with an ``exclude_categories=["diagnostic"]`` +
    ``allow_entity_ids=["person.allowed"]`` config, this is the shared fixture the
    outbound-scan scrub tests below render payloads against.
    """
    return FakeClient(
        registry={
            "success": True,
            "result": [
                {"entity_id": "person.allowed", "entity_category": None},
                {
                    "entity_id": "device_tracker.hidden_a",
                    "entity_category": "diagnostic",
                },
                {
                    "entity_id": "device_tracker.hidden_b",
                    "entity_category": "diagnostic",
                },
            ],
        },
        states=[
            {"entity_id": "person.allowed", "attributes": {}},
            {"entity_id": "device_tracker.hidden_a", "attributes": {}},
            {"entity_id": "device_tracker.hidden_b", "attributes": {}},
        ],
    )


class TestOutboundScan:
    async def test_get_state_omits_hidden_content_with_warning_and_log(
        self, set_config, caplog
    ):
        set_config(
            enabled=True,
            enforce=True,
            exclude_categories=["diagnostic"],
            allow_entity_ids=["person.allowed"],
        )
        client = FakeClient(
            registry={
                "success": True,
                "result": [
                    {"entity_id": "person.allowed", "entity_category": None},
                    {
                        "entity_id": "device_tracker.hidden_a",
                        "entity_category": "diagnostic",
                    },
                    {
                        "entity_id": "device_tracker.hidden_b",
                        "entity_category": "diagnostic",
                    },
                ],
            },
            states=[
                {"entity_id": "person.allowed", "attributes": {}},
                {"entity_id": "device_tracker.hidden_a", "attributes": {}},
                {"entity_id": "device_tracker.hidden_b", "attributes": {}},
            ],
        )
        payload = {
            "data": {
                "entity_id": "person.allowed",
                "state": "home",
                "attributes": {
                    "source": "device_tracker.hidden_a",
                    "device_tracker.hidden_b": "mapping key is hidden too",
                    "device_trackers": [
                        "device_tracker.hidden_a",
                        "device_tracker.hidden_b",
                    ],
                    "friendly_name": "Allowed person",
                },
            },
            "metadata": {"time_zone": "UTC"},
        }
        mw = make_mw(get_client=lambda: client)
        caplog.set_level("INFO", logger=enforcement.__name__)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "person.allowed"}),
            _returns(tool_result),
        )

        structured = result.structured_content
        text = json.loads(result.content[0].text)
        for rendered in (structured, text):
            # Attribute values derive from the hidden trackers (coordinates would
            # be theirs), so the whole mapping goes, not just the naming fields.
            assert "attributes" not in rendered["data"]
            assert rendered["data"]["state"] == "home"
            assert rendered["warnings"] == [
                enforcement._state_content_warning(["data.attributes"])
            ]
            assert "device_tracker.hidden" not in json.dumps(rendered)
            assert "Entity Visibility section" in rendered["warnings"][0]
        scrub_logs = [
            record
            for record in caplog.records
            if record.msg.startswith("visibility enforce: scrubbed")
        ]
        # One omitted path per representation, both representations mutated.
        assert [record.args for record in scrub_logs] == [(2, 2)]

    async def test_get_state_drops_whole_record_naming_hidden_entity(self, set_config):
        """A nested record that NAMES a hidden id is dropped whole, not field-wise.

        Its sibling fields (a friendly_name here) would otherwise leak the hidden
        entity's identity even after the ``entity_id`` itself was removed.
        """
        set_config(
            enabled=True,
            enforce=True,
            exclude_categories=["diagnostic"],
            allow_entity_ids=["person.allowed"],
        )
        payload = {
            "data": {
                "entity_id": "person.allowed",
                "state": "home",
                "attributes": {"friendly_name": "Allowed person"},
                "related": [
                    {
                        "entity_id": "device_tracker.hidden_a",
                        "state": "home",
                        "friendly_name": "Secret",
                    }
                ],
            }
        }
        mw = make_mw(get_client=_hidden_tracker_client)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "person.allowed"}),
            _returns(tool_result),
        )

        for rendered in (
            result.structured_content,
            json.loads(result.content[0].text),
        ):
            assert rendered["data"]["related"] == []
            assert rendered["data"]["attributes"] == {"friendly_name": "Allowed person"}
            assert "Secret" not in json.dumps(rendered)
            assert rendered["warnings"] == [
                enforcement._state_content_warning(["data.related[0]"])
            ]

    async def test_get_state_hidden_mapping_key_is_not_named_in_warning(
        self, set_config
    ):
        """A hidden id used as a mapping key is reported as a placeholder path."""
        set_config(
            enabled=True,
            enforce=True,
            exclude_categories=["diagnostic"],
            allow_entity_ids=["person.allowed"],
        )
        payload = {
            "data": {
                "entity_id": "person.allowed",
                "state": "home",
                "device_tracker.hidden_a": "seen",
            }
        }
        mw = make_mw(get_client=_hidden_tracker_client)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "person.allowed"}),
            _returns(tool_result),
        )

        for rendered in (
            result.structured_content,
            json.loads(result.content[0].text),
        ):
            assert rendered["data"] == {"entity_id": "person.allowed", "state": "home"}
            assert rendered["warnings"] == [
                enforcement._state_content_warning(["data.<hidden key>"])
            ]
            assert "device_tracker.hidden" not in json.dumps(rendered)

    async def test_get_state_refuses_when_only_one_representation_can_carry_warning(
        self, set_config
    ):
        """A list-root text block beside a dict-root structured payload refuses.

        The scrub must land identically on BOTH representations. A JSON list root
        cannot carry the warning key, so scrubbing only the dict half would ship a
        cleaned structured payload next to an unscrubbed text block.
        """
        set_config(
            enabled=True,
            enforce=True,
            exclude_categories=["diagnostic"],
            allow_entity_ids=["person.allowed"],
        )
        list_payload = [
            {
                "entity_id": "person.allowed",
                "attributes": {"source": "device_tracker.hidden_a"},
            }
        ]
        dict_payload = {
            "data": {
                "entity_id": "person.allowed",
                "attributes": {"source": "device_tracker.hidden_a"},
            }
        }
        mw = make_mw(get_client=_hidden_tracker_client)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(list_payload))],
            structured_content=dict_payload,
        )

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "person.allowed"}),
                _returns(tool_result),
            )

        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_get_state_scrub_warning_appends_to_existing_warnings(
        self, set_config
    ):
        """The scrub warning is APPENDED — a tool's own warnings must survive."""
        set_config(
            enabled=True,
            enforce=True,
            exclude_categories=["diagnostic"],
            allow_entity_ids=["person.allowed"],
        )
        payload = {
            "data": {
                "entity_id": "person.allowed",
                "attributes": {
                    "source": "device_tracker.hidden_a",
                    "friendly_name": "Allowed person",
                },
            },
            "warnings": ["existing"],
        }
        mw = make_mw(get_client=_hidden_tracker_client)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "person.allowed"}),
            _returns(tool_result),
        )

        for rendered in (
            result.structured_content,
            json.loads(result.content[0].text),
        ):
            assert "attributes" not in rendered["data"]
            assert rendered["warnings"] == [
                "existing",
                enforcement._state_content_warning(["data.attributes"]),
            ]

    async def test_get_state_bulk_omits_hidden_content(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        payload = {
            "success": True,
            "states": {
                "person.allowed": {
                    "entity_id": "person.allowed",
                    "attributes": {
                        "source": "sensor.hidden",
                        "friendly_name": "Allowed person",
                    },
                }
            },
        }
        mw = make_mw(get_client=FakeClient)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": ["person.allowed"]}),
            _returns(tool_result),
        )

        for rendered in (
            result.structured_content,
            json.loads(result.content[0].text),
        ):
            assert "attributes" not in rendered["states"]["person.allowed"]
            assert rendered["warnings"] == [
                enforcement._state_content_warning(["states.person.allowed.attributes"])
            ]

    async def test_get_state_refuses_when_scrub_warning_cannot_be_attached(
        self, set_config
    ):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        payload = {
            "data": {"attributes": {"source": "sensor.hidden"}},
            "warnings": "unexpected non-list warning payload",
        }
        mw = make_mw(get_client=FakeClient)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "person.allowed"}),
                _returns(tool_result),
            )

        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_get_state_refuses_list_root_that_cannot_carry_warning(
        self, set_config
    ):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        payload = [
            {
                "entity_id": "person.allowed",
                "attributes": {"source": "sensor.hidden"},
            }
        ]
        mw = make_mw(get_client=FakeClient)
        tool_result = ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
        )

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "person.allowed"}),
                _returns(tool_result),
            )

        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_get_state_non_json_hidden_reference_still_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.allowed"}),
                _returns(text_result("relationship: sensor.foo")),
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_text_content_hit_refused_without_naming_entity(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)
        result = await mw.on_call_tool(
            make_context("ha_get_logs", {}),
            _returns(text_result("nothing restricted here", structured={"ok": True})),
        )
        assert result.content[0].text == "nothing restricted here"

    async def test_clean_tool_error_passes_through_unmodified(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=FakeClient)

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
    @pytest.mark.parametrize(
        ("proxy_name", "target_name", "arguments"),
        [
            (
                "ha_call_read_tool",
                "ha_config_get_dashboard",
                {"include_screenshot": True},
            ),
            (
                "ha_call_write_tool",
                "ha_config_set_dashboard",
                {"return_screenshot": True},
            ),
            ("ha_call_write_tool", "ha_manage_custom_tool", {"code": "print(1)"}),
            ("ha_call_write_tool", "ha_manage_custom_tool", {"run_saved": "t"}),
            (
                "ha_call_read_tool",
                "ha_config_get_dashboard",
                json.dumps({"include_screenshot": True}),
            ),
        ],
    )
    async def test_proxied_unscannable_call_refused_before_dispatch(
        self, set_config, proxy_name, target_name, arguments
    ):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    proxy_name,
                    {"name": target_name, "arguments": arguments},
                ),
                _unreached_call_next,
            )

        body = _error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert body["tool_name"] == target_name

    async def test_custom_tool_code_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_manage_custom_tool", {"code": "print(1)"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_custom_tool_run_saved_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_manage_custom_tool", {"run_saved": "t"}),
                _unreached_call_next,
            )

    async def test_custom_tool_list_saved_allowed(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        result = await mw.on_call_tool(
            make_context("ha_manage_custom_tool", {"list_saved": True}),
            _returns(text_result("saved tools list")),
        )
        assert result.content[0].text == "saved tools list"

    async def test_screenshot_tool_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_dashboard_screenshot", {"dashboard": "x"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_set_dashboard_return_screenshot_refused(self, set_config):
        # The dashboard WRITE tool shares the native-image capture path:
        # return_screenshot hands back pixels no text scan can inspect.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context(
                    "ha_config_set_dashboard",
                    {"url_path": "clean-dash", "return_screenshot": True},
                ),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_set_dashboard_without_screenshot_allowed(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        result = await mw.on_call_tool(
            make_context("ha_config_set_dashboard", {"url_path": "clean-dash"}),
            _returns(text_result("dashboard saved")),
        )
        assert result.content[0].text == "dashboard saved"

    async def test_dashboard_with_screenshot_refused(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_config_get_dashboard", {"include_screenshot": True}),
                _unreached_call_next,
            )

    async def test_plain_dashboard_read_allowed_and_scanned(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.foo"])
        mw = make_mw(get_client=FakeClient)
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
        mw = make_mw(get_client=lambda: client)
        for _ in range(3):
            await mw.on_call_tool(
                make_context("ha_get_overview", {}), _returns(text_result("clean"))
            )
        assert client.get_states_calls == 1  # one refresh, then cache hits

    async def test_cache_invalidated_on_config_change(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.a"])
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)
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
        mw = make_mw(get_client=lambda: client)
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
        mw = make_mw(get_client=lambda: client)
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
        mw = make_mw(get_client=lambda: client)
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


# ---------------------------------------------------------------------------
# Collection-read scrub seam (ha_search config-body branch)
# ---------------------------------------------------------------------------


class TestScrubSeam:
    def test_scrub_records_drops_embedded_hidden_id(self):
        regex = _build_hidden_regex({"input_boolean.hidden_probe"})
        records = [
            {"entity_id": "automation.morning", "config": {"trigger": "sun"}},
            {
                "entity_id": "automation.night",
                "config": {"action": {"entity_id": "input_boolean.hidden_probe"}},
            },
            {"friendly_name": "Hidden Probe", "id": "input_boolean.hidden_probe"},
        ]
        kept = scrub_records(records, regex)
        assert [r.get("entity_id", r.get("id")) for r in kept] == ["automation.morning"]

    def test_scrub_records_boundary_no_false_positive(self):
        regex = _build_hidden_regex({"sensor.foo"})
        records = [{"entity_id": "sensor.foo2"}, {"entity_id": "my_sensor.foo"}]
        assert scrub_records(records, regex) == records

    async def test_active_hidden_regex_none_when_enforce_off(self, set_config):
        set_config(enabled=True, enforce=False, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        assert await active_hidden_regex(client) is None
        assert client.get_states_calls == 0  # inactive: no hidden-set fetch

    async def test_active_hidden_regex_uses_shared_cache(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)
        # Middleware call primes the module-shared TTL cache...
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        assert client.get_states_calls == 1
        # ...and the scrub seam reuses it: even a DEAD client works because no
        # refresh is needed. Regression for CI round 3, where the scrub reached
        # a cold cache through a stale server instance's closed client and
        # skipped the scrub entirely.
        regex = await active_hidden_regex(FakeClient(fail=True))
        assert regex is not None
        assert regex.search("states.sensor.hidden reference")
        assert not regex.search("sensor.hidden2")
        assert client.get_states_calls == 1

    async def test_active_hidden_regex_refreshes_with_caller_client(self, set_config):
        # Cold cache: the scrub refreshes using the CALLER's live client.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        client = FakeClient()
        regex = await active_hidden_regex(client)
        assert regex is not None
        assert client.get_states_calls == 1

    async def test_active_hidden_regex_fail_soft_on_data_error(self, set_config):
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        assert await active_hidden_regex(FakeClient(fail=True)) is None


# ---------------------------------------------------------------------------
# Codex round-1 regressions: stale-config fallback + strict device registry
# ---------------------------------------------------------------------------


class TestFallbackScoping:
    async def test_last_known_good_not_reused_across_config_change(self, set_config):
        # A hidden set cached under denylist A is a DIFFERENT policy than
        # denylist B: when the config changes while the registry refresh is
        # failing, the middleware must fail closed, not serve A's set.
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.a"])
        client = FakeClient()
        mw = make_mw(get_client=lambda: client)
        await mw.on_call_tool(
            make_context("ha_get_overview", {}), _returns(text_result("clean"))
        )
        assert client.get_states_calls == 1
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.b"])
        client.fail = True
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.b"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_unusable_device_registry_fails_closed_for_area_dimension(
        self, set_config
    ):
        # An area exclude NEEDS the device registry (device-bound entities
        # inherit their device's area). The resolver's device parser fails
        # open to empty maps even under strict, so the middleware must treat
        # a returned-but-unusable payload as data-unavailable.
        set_config(enabled=True, enforce=True, exclude_areas=["bedroom"])
        client = FakeClient(device={"success": False})
        mw = make_mw(get_client=lambda: client)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_null_device_registry_entry_fails_closed(self, set_config):
        # A degenerate success payload ({"result": [null]}) passes the shape
        # check but parses to empty device maps — per-entry validation must
        # treat it as unavailable rather than silently dropping device-
        # inherited area/label denies (Codex round 2, P2).
        set_config(enabled=True, enforce=True, exclude_areas=["bedroom"])
        client = FakeClient(device={"success": True, "result": [None]})
        mw = make_mw(get_client=lambda: client)
        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _unreached_call_next,
            )
        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"

    async def test_missing_device_parent_fails_closed_for_area_dimension(
        self, set_config
    ):
        set_config(enabled=True, enforce=True, exclude_areas=["bedroom"])
        client = FakeClient(
            registry={
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.orphan",
                        "area_id": None,
                        "device_id": "child",
                    }
                ],
            },
            device={
                "success": True,
                "result": [
                    {
                        "id": "child",
                        "area_id": None,
                        "parent_device_id": "missing",
                    }
                ],
            },
        )
        mw = make_mw(get_client=lambda: client)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.orphan"}),
                _unreached_call_next,
            )

        assert _error_body(exc)["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


class TestComponentBucketScrub:
    async def test_component_config_buckets_scrubbed_and_totals_adjusted(
        self, set_config
    ):
        from ha_mcp.tools.tools_search import _scrub_component_config_buckets

        set_config(
            enabled=True,
            enforce=True,
            deny_entity_ids=["input_boolean.hidden_probe"],
        )
        client = FakeClient()
        response = {
            "entities": [],
            "automations": [],
            "scripts": [],
            "scenes": [],
            "helpers": [
                {"entity_id": "input_boolean.hidden_probe", "name": "Hidden"},
                {"entity_id": "input_boolean.visible", "name": "Visible"},
            ],
            "dashboards": [],
            "config_total_matches": 2,
            "count": 2,
        }
        await _scrub_component_config_buckets(response, client)
        assert [r["entity_id"] for r in response["helpers"]] == [
            "input_boolean.visible"
        ]
        assert response["config_total_matches"] == 1
        assert response["count"] == 1

    async def test_component_scrub_noop_when_enforce_off(self, set_config):
        from ha_mcp.tools.tools_search import _scrub_component_config_buckets

        set_config(
            enabled=True,
            enforce=False,
            deny_entity_ids=["input_boolean.hidden_probe"],
        )
        response = {
            "helpers": [{"entity_id": "input_boolean.hidden_probe"}],
            "config_total_matches": 1,
            "count": 1,
        }
        await _scrub_component_config_buckets(response, FakeClient())
        assert response["helpers"] == [{"entity_id": "input_boolean.hidden_probe"}]
        assert response["config_total_matches"] == 1
