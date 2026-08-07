"""Unit tests for schema-driven secret redaction (issue 2157, redaction.py)."""

import json
import typing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.redaction import (
    REDACTED_EMPTY,
    REDACTED_KNOWN,
    REDACTED_SET,
    RedactSecretsMiddleware,
    _clear_known_secret_values,
    collect_addon_secret_values,
    is_password_flow_field,
    is_sentinel,
    known_secret_values,
    redact_addon_options,
    redact_flow_schema,
    redact_options_by_flow_schema,
    register_known_secret_values,
    scrub_obj,
    scrub_text,
    sentinel_for,
    sentinel_option_keys,
)
from ha_mcp.tools.config_entry_flow import _reject_redaction_sentinels
from ha_mcp.tools.tools_integrations import options_from_form_flow


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the module-level known-secrets set per test."""
    _clear_known_secret_values()
    yield
    _clear_known_secret_values()


@pytest.fixture
def redact_on(monkeypatch):
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True),
    )


@pytest.fixture
def redact_off(monkeypatch):
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=False),
    )


# The issue's motivating shape: a GitHub PAT marked format=password next to
# ordinary fields, plus a nested group with its own password leaf.
ADDON_SCHEMA = [
    {"name": "github_pat", "required": True, "type": "string", "format": "password"},
    {"name": "log_level", "type": "list", "options": ["info", "debug"]},
    {"name": "empty_password", "type": "string", "format": "password"},
    {
        "name": "ssh",
        "type": "schema",
        "schema": [
            {"name": "username", "type": "string"},
            {"name": "password", "type": "string", "format": "password"},
        ],
    },
    {
        "name": "accounts",
        "type": "schema",
        "multiple": True,
        "schema": [
            {"name": "user", "type": "string"},
            {"name": "token", "type": "string", "format": "password"},
        ],
    },
]

ADDON_OPTIONS = {
    "github_pat": "github_pat_LIVESECRETVALUE",
    "log_level": "info",
    "empty_password": "",
    "unlisted_key": "not in schema",
    "ssh": {"username": "root", "password": "hunter2secret"},
    "accounts": [
        {"user": "a", "token": "tok_aaaaaaaa"},
        {"user": "b", "token": ""},
    ],
}


class TestRedactAddonOptions:
    def test_password_value_becomes_set_sentinel(self):
        out = redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        assert out["github_pat"] == REDACTED_SET

    def test_empty_password_becomes_empty_sentinel(self):
        out = redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        assert out["empty_password"] == REDACTED_EMPTY

    def test_non_password_and_unlisted_keys_untouched(self):
        out = redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        assert out["log_level"] == "info"
        assert out["unlisted_key"] == "not in schema"

    def test_nested_group_password_redacted(self):
        out = redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        assert out["ssh"] == {"username": "root", "password": REDACTED_SET}

    def test_nested_group_list_password_redacted_per_item(self):
        out = redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        assert out["accounts"] == [
            {"user": "a", "token": REDACTED_SET},
            {"user": "b", "token": REDACTED_EMPTY},
        ]

    def test_input_options_not_mutated(self):
        before = json.dumps(ADDON_OPTIONS, sort_keys=True)
        redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        assert json.dumps(ADDON_OPTIONS, sort_keys=True) == before

    def test_non_dict_options_passthrough(self):
        assert redact_addon_options(None, ADDON_SCHEMA) is None
        assert redact_addon_options("x", ADDON_SCHEMA) == "x"

    def test_malformed_schema_passthrough(self):
        assert redact_addon_options(ADDON_OPTIONS, None) == ADDON_OPTIONS
        assert redact_addon_options(ADDON_OPTIONS, "bogus") == ADDON_OPTIONS

    def test_multiple_password_list_keeps_container_shape(self):
        schema = [
            {"name": "tokens", "type": "string", "format": "password", "multiple": True}
        ]
        options = {"tokens": ["tok_one_11", "", "tok_two_22"]}
        out = redact_addon_options(options, schema)
        assert out["tokens"] == [REDACTED_SET, REDACTED_EMPTY, REDACTED_SET]

    def test_redaction_is_idempotent(self):
        once = redact_addon_options(ADDON_OPTIONS, ADDON_SCHEMA)
        twice = redact_addon_options(once, ADDON_SCHEMA)
        assert twice == once


class TestCollectAddonSecretValues:
    def test_collects_all_nonempty_password_values(self):
        values = collect_addon_secret_values(ADDON_OPTIONS, ADDON_SCHEMA)
        assert values == {
            "github_pat_LIVESECRETVALUE",
            "hunter2secret",
            "tok_aaaaaaaa",
        }

    def test_ignores_empty_and_non_password_values(self):
        values = collect_addon_secret_values(ADDON_OPTIONS, ADDON_SCHEMA)
        assert "" not in values
        assert "info" not in values
        assert "root" not in values


class TestSentinels:
    def test_sentinel_for_set_and_empty(self):
        assert sentinel_for("value") == REDACTED_SET
        assert sentinel_for(True) == REDACTED_SET
        assert sentinel_for(0) == REDACTED_SET
        assert sentinel_for("") == REDACTED_EMPTY
        assert sentinel_for(None) == REDACTED_EMPTY
        assert sentinel_for([]) == REDACTED_EMPTY

    def test_is_sentinel(self):
        assert is_sentinel(REDACTED_SET)
        assert is_sentinel(REDACTED_EMPTY)
        assert is_sentinel(REDACTED_KNOWN)
        assert not is_sentinel("plain")
        assert not is_sentinel(None)

    def test_sentinel_option_keys_flat_nested_and_list(self):
        options = {
            "a": REDACTED_SET,
            "b": "fine",
            "nested": {"c": REDACTED_EMPTY},
            "items": ["ok", REDACTED_KNOWN, {"d": REDACTED_SET}],
        }
        assert sentinel_option_keys(options) == [
            "a",
            "nested.c",
            "items[1]",
            "items[2].d",
        ]

    def test_sentinel_option_keys_clean_options(self):
        assert sentinel_option_keys({"a": 1, "b": {"c": "x"}}) == []


FLOW_SCHEMA = [
    {"name": "host", "type": "string", "description": {"suggested_value": "1.2.3.4"}},
    {
        "name": "api_key",
        "selector": {"text": {"type": "password"}},
        "description": {"suggested_value": "sk-LIVEFLOWSECRET"},
    },
    {"name": "legacy_pw", "type": "password", "default": "legacypw99"},
    {
        "name": "advanced",
        "type": "expandable",
        "schema": [
            {
                "name": "inner_token",
                "selector": {"text": {"type": "password"}},
                "description": {"suggested_value": "tok-INNERSECRET"},
            }
        ],
    },
]


class TestPasswordFlowFields:
    def test_text_selector_password_detected(self):
        assert is_password_flow_field(FLOW_SCHEMA[1])

    def test_literal_password_type_detected(self):
        assert is_password_flow_field(FLOW_SCHEMA[2])

    def test_plain_fields_not_detected(self):
        assert not is_password_flow_field(FLOW_SCHEMA[0])
        assert not is_password_flow_field(
            {"name": "x", "selector": {"text": {"type": "email"}}}
        )


class TestRedactFlowSchema:
    def test_password_suggested_values_and_defaults_redacted(self):
        out = redact_flow_schema(FLOW_SCHEMA)
        assert out[1]["description"]["suggested_value"] == REDACTED_SET
        assert out[2]["default"] == REDACTED_SET
        assert out[3]["schema"][0]["description"]["suggested_value"] == REDACTED_SET

    def test_non_password_values_kept_and_input_unmutated(self):
        before = json.dumps(FLOW_SCHEMA, sort_keys=True)
        out = redact_flow_schema(FLOW_SCHEMA)
        assert out[0]["description"]["suggested_value"] == "1.2.3.4"
        assert json.dumps(FLOW_SCHEMA, sort_keys=True) == before

    def test_harvests_replaced_values(self):
        redact_flow_schema(FLOW_SCHEMA)
        assert {"sk-LIVEFLOWSECRET", "legacypw99", "tok-INNERSECRET"} <= set(
            known_secret_values()
        )


class TestRedactOptionsByFlowSchema:
    def test_marked_keys_redacted_others_kept(self):
        options = {"host": "1.2.3.4", "api_key": "sk-LIVEFLOWSECRET", "extra": "x"}
        out = redact_options_by_flow_schema(options, FLOW_SCHEMA)
        assert out == {
            "host": "1.2.3.4",
            "api_key": REDACTED_SET,
            "extra": "x",
        }
        assert "sk-LIVEFLOWSECRET" in known_secret_values()

    def test_non_dict_or_no_schema_passthrough(self):
        assert redact_options_by_flow_schema(None, FLOW_SCHEMA) is None
        options = {"api_key": "keep"}
        assert redact_options_by_flow_schema(options, None) == options

    def test_nested_section_raw_copy_redacted(self):
        # Sections are additively flattened: the leaf appears BOTH at the
        # top level and inside the raw nested dict — both copies must be
        # redacted.
        options = {
            "inner_token": "tok-INNERSECRET",
            "advanced": {"inner_token": "tok-INNERSECRET"},
        }
        out = redact_options_by_flow_schema(options, FLOW_SCHEMA)
        assert out["inner_token"] == REDACTED_SET
        assert out["advanced"] == {"inner_token": REDACTED_SET}

    def test_redaction_is_idempotent(self):
        options = {"api_key": "sk-LIVEFLOWSECRET", "empty_pw": ""}
        once = redact_options_by_flow_schema(options, FLOW_SCHEMA)
        twice = redact_options_by_flow_schema(once, FLOW_SCHEMA)
        assert twice == once


class TestOptionsFromFormFlowRedaction:
    def _flow(self):
        return {"data_schema": FLOW_SCHEMA}

    def test_toggle_on_redacts_password_fields(self, redact_on):
        out = options_from_form_flow(self._flow())
        assert out["host"] == "1.2.3.4"
        assert out["api_key"] == REDACTED_SET
        assert out["legacy_pw"] == REDACTED_SET
        assert out["inner_token"] == REDACTED_SET
        assert "sk-LIVEFLOWSECRET" in known_secret_values()

    def test_toggle_off_returns_legacy_values(self, redact_off):
        out = options_from_form_flow(self._flow())
        assert out["api_key"] == "sk-LIVEFLOWSECRET"
        assert out["legacy_pw"] == "legacypw99"
        assert known_secret_values() == ()


class TestKnownSecretRegistry:
    def test_short_values_never_registered(self):
        register_known_secret_values(["abc", "12345", "long-enough-secret"])
        assert known_secret_values() == ("long-enough-secret",)

    def test_non_strings_ignored(self):
        register_known_secret_values([None, 123456, "real-secret-value"])  # type: ignore[list-item]
        assert known_secret_values() == ("real-secret-value",)

    def test_snapshot_ordered_longest_first(self):
        register_known_secret_values(["shorter-1", "the-much-longer-secret"])
        assert known_secret_values() == ("the-much-longer-secret", "shorter-1")


class TestScrub:
    def test_scrub_text_replaces_all_occurrences(self):
        out = scrub_text("token=SECRETVALUE1 again SECRETVALUE1", ("SECRETVALUE1",))
        assert out == f"token={REDACTED_KNOWN} again {REDACTED_KNOWN}"

    def test_scrub_text_longest_first(self):
        # The longer secret contains the shorter one; with the longest-first
        # order the registry provides, scrubbing must not leave fragments of
        # the longer secret behind.
        register_known_secret_values(["SECRET-x", "SECRET-xLONGER"])
        out = scrub_text("a SECRET-xLONGER b SECRET-x c", known_secret_values())
        assert out == f"a {REDACTED_KNOWN} b {REDACTED_KNOWN} c"

    def test_scrub_obj_walks_dicts_lists_and_keys(self):
        obj = {
            "note": "uses SECRETVALUE1 here",
            "SECRETVALUE1": "as key",
            "nested": [{"v": "SECRETVALUE1"}, 42, None],
        }
        out = scrub_obj(obj, ("SECRETVALUE1",))
        assert out == {
            "note": f"uses {REDACTED_KNOWN} here",
            REDACTED_KNOWN: "as key",
            "nested": [{"v": REDACTED_KNOWN}, 42, None],
        }


class TestRejectRedactionSentinels:
    def test_toggle_on_rejects_sentinel_config(self, redact_on):
        with pytest.raises(ToolError) as exc_info:
            _reject_redaction_sentinels({"password": REDACTED_SET})
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "password" in body["error"]["message"]

    def test_toggle_on_accepts_clean_config(self, redact_on):
        _reject_redaction_sentinels({"password": "real-value"})

    def test_toggle_off_still_rejects(self, redact_off):
        # The guard is deliberately NOT gated on the toggle: a sentinel
        # captured while redaction was on must not overwrite a credential
        # after the operator turns it off.
        with pytest.raises(ToolError):
            _reject_redaction_sentinels({"password": REDACTED_SET})


def _tool_result(text: str, structured: dict | None):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        structured_content=structured,
    )


class TestRedactSecretsMiddleware:
    async def _call(self, result):
        middleware = RedactSecretsMiddleware()
        return await middleware.on_call_tool(
            MagicMock(), AsyncMock(return_value=result)
        )

    @pytest.mark.asyncio
    async def test_toggle_off_passthrough(self, redact_off):
        register_known_secret_values(["SECRETVALUE1"])
        result = _tool_result("has SECRETVALUE1", {"v": "SECRETVALUE1"})
        out = await self._call(result)
        assert out.content[0].text == "has SECRETVALUE1"
        assert out.structured_content == {"v": "SECRETVALUE1"}

    @pytest.mark.asyncio
    async def test_toggle_on_scrubs_content_and_structured(self, redact_on):
        register_known_secret_values(["SECRETVALUE1"])
        result = _tool_result("text SECRETVALUE1", {"v": "SECRETVALUE1", "ok": "clean"})
        out = await self._call(result)
        assert out.content[0].text == f"text {REDACTED_KNOWN}"
        assert out.structured_content == {"v": REDACTED_KNOWN, "ok": "clean"}

    @pytest.mark.asyncio
    async def test_toggle_on_no_known_secrets_passthrough(self, redact_on):
        result = _tool_result("plain", {"v": "plain"})
        out = await self._call(result)
        assert out.content[0].text == "plain"
        assert out.structured_content == {"v": "plain"}

    @pytest.mark.asyncio
    async def test_tool_error_args_scrubbed(self, redact_on):
        register_known_secret_values(["SECRETVALUE1"])
        middleware = RedactSecretsMiddleware()
        call_next = AsyncMock(side_effect=ToolError("failed with SECRETVALUE1"))
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(MagicMock(), call_next)
        assert str(exc_info.value) == f"failed with {REDACTED_KNOWN}"

    @pytest.mark.asyncio
    async def test_tool_error_untouched_when_off(self, redact_off):
        register_known_secret_values(["SECRETVALUE1"])
        middleware = RedactSecretsMiddleware()
        call_next = AsyncMock(side_effect=ToolError("failed with SECRETVALUE1"))
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(MagicMock(), call_next)
        assert str(exc_info.value) == "failed with SECRETVALUE1"

    @pytest.mark.asyncio
    async def test_json_escaped_form_scrubbed_from_text_block(self, redact_on):
        # FastMCP serializes the structured dict into the text block, so a
        # secret containing a quote appears there JSON-escaped — the scrub
        # must catch that form too.
        secret = 'pass"word\\x'
        register_known_secret_values([secret])
        escaped = json.dumps(secret)[1:-1]
        assert escaped != secret
        result = _tool_result(f'{{"pw": "{escaped}"}}', {"pw": secret})
        out = await self._call(result)
        assert out.content[0].text == f'{{"pw": "{REDACTED_KNOWN}"}}'
        assert out.structured_content == {"pw": REDACTED_KNOWN}

    @pytest.mark.asyncio
    async def test_generic_exception_args_scrubbed(self, redact_on):
        # FastMCP forwards non-ToolError exception text to clients too
        # (mask_error_details is not enabled) — those must be scrubbed while
        # preserving the exception type.
        register_known_secret_values(["SECRETVALUE1"])
        middleware = RedactSecretsMiddleware()
        call_next = AsyncMock(side_effect=ValueError("boom SECRETVALUE1"))
        with pytest.raises(ValueError) as exc_info:
            await middleware.on_call_tool(MagicMock(), call_next)
        assert str(exc_info.value) == f"boom {REDACTED_KNOWN}"


class TestRedactComponentOptions:
    """IntegrationTools._redact_component_options — component path (issue 2157)."""

    def _tools_with_flow(self, flow_response, abort_raises=False):
        from ha_mcp.tools.tools_integrations import IntegrationTools

        client = MagicMock()
        client.start_options_flow = AsyncMock(return_value=flow_response)
        client.abort_options_flow = (
            AsyncMock(side_effect=RuntimeError("abort failed"))
            if abort_raises
            else AsyncMock()
        )
        return IntegrationTools(client)

    @pytest.mark.asyncio
    async def test_form_schema_redacts_marked_fields(self):
        tools = self._tools_with_flow(
            {"flow_id": "f1", "type": "form", "data_schema": FLOW_SCHEMA}
        )
        entry = {
            "entry_id": "e1",
            "supports_options": True,
            "options": {"host": "1.2.3.4", "api_key": "sk-LIVEFLOWSECRET"},
        }
        warnings: list[str] = []
        await tools._redact_component_options("e1", entry, warnings)
        assert entry["options"] == {"host": "1.2.3.4", "api_key": REDACTED_SET}
        assert warnings == []
        tools._client.abort_options_flow.assert_awaited_once_with("f1")

    @pytest.mark.asyncio
    async def test_menu_flow_fails_closed_with_warning(self):
        tools = self._tools_with_flow(
            {"flow_id": "f1", "type": "menu", "menu_options": ["a"]}
        )
        entry = {
            "entry_id": "e1",
            "supports_options": True,
            "options": {"host": "1.2.3.4", "maybe_pw": "hunter2secret", "empty": ""},
        }
        warnings: list[str] = []
        await tools._redact_component_options("e1", entry, warnings)
        assert entry["options"] == {
            "host": REDACTED_SET,
            "maybe_pw": REDACTED_SET,
            "empty": REDACTED_EMPTY,
        }
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_probe_failure_fails_closed_with_warning(self):
        from ha_mcp.tools.tools_integrations import IntegrationTools

        client = MagicMock()
        client.start_options_flow = AsyncMock(side_effect=RuntimeError("boom"))
        client.abort_options_flow = AsyncMock()
        tools = IntegrationTools(client)
        entry = {
            "entry_id": "e1",
            "supports_options": True,
            "options": {"api_key": "sk-LIVEFLOWSECRET"},
        }
        warnings: list[str] = []
        await tools._redact_component_options("e1", entry, warnings)
        assert entry["options"] == {"api_key": REDACTED_SET}
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_no_options_flow_passes_through(self):
        tools = self._tools_with_flow({"flow_id": "f1", "type": "form"})
        entry = {
            "entry_id": "e1",
            "supports_options": False,
            "options": {"plain": "value"},
        }
        warnings: list[str] = []
        await tools._redact_component_options("e1", entry, warnings)
        assert entry["options"] == {"plain": "value"}
        assert warnings == []
        tools._client.start_options_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_options_skip_probe(self):
        tools = self._tools_with_flow({"flow_id": "f1", "type": "form"})
        entry = {"entry_id": "e1", "supports_options": True, "options": {}}
        await tools._redact_component_options("e1", entry, [])
        assert entry["options"] == {}
        tools._client.start_options_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_abort_failure_does_not_break_redaction(self):
        tools = self._tools_with_flow(
            {"flow_id": "f1", "type": "form", "data_schema": FLOW_SCHEMA},
            abort_raises=True,
        )
        entry = {
            "entry_id": "e1",
            "supports_options": True,
            "options": {"api_key": "sk-LIVEFLOWSECRET"},
        }
        warnings: list[str] = []
        await tools._redact_component_options("e1", entry, warnings)
        assert entry["options"] == {"api_key": REDACTED_SET}
        assert warnings == []


class TestEmbeddedSentinelRejection:
    """Strings CONTAINING a sentinel are rejected, not only exact matches."""

    def test_sentinel_option_keys_flags_embedded_marker(self):
        options = {
            "dsn": f"postgres://u:{REDACTED_KNOWN}@db/x",
            "ok": "clean-value",
            "items": [f"prefix {REDACTED_SET} suffix"],
        }
        assert sentinel_option_keys(options) == ["dsn", "items[0]"]

    def test_reject_helper_rejects_embedded_marker(self, redact_on):
        with pytest.raises(ToolError):
            _reject_redaction_sentinels({"dsn": f"postgres://u:{REDACTED_KNOWN}@db"})


class TestMultiValuePasswordFields:
    """multiple:true password selectors carry list values (issue 2157)."""

    def test_flow_schema_list_suggested_value_harvested_and_shape_kept(self):
        schema = [
            {
                "name": "tokens",
                "selector": {"text": {"type": "password", "multiple": True}},
                "description": {"suggested_value": ["tok-AAA111", "tok-BBB222"]},
            }
        ]
        out = redact_flow_schema(schema)
        assert out[0]["description"]["suggested_value"] == [
            REDACTED_SET,
            REDACTED_SET,
        ]
        assert {"tok-AAA111", "tok-BBB222"} <= set(known_secret_values())

    def test_options_from_form_flow_list_value(self, redact_on):
        flow = {
            "data_schema": [
                {
                    "name": "tokens",
                    "selector": {"text": {"type": "password", "multiple": True}},
                    "description": {"suggested_value": ["tok-AAA111", ""]},
                }
            ]
        }
        out = options_from_form_flow(flow)
        assert out["tokens"] == [REDACTED_SET, REDACTED_EMPTY]
        assert "tok-AAA111" in known_secret_values()

    def test_redact_options_by_flow_schema_list_value(self):
        schema = [
            {"name": "tokens", "selector": {"text": {"type": "password"}}},
        ]
        out = redact_options_by_flow_schema({"tokens": ["tok-AAA111", ""]}, schema)
        assert out["tokens"] == [REDACTED_SET, REDACTED_EMPTY]
        assert "tok-AAA111" in known_secret_values()


class TestWriteFlowErrorSchemaRedaction:
    """Flow 4xx error contexts must not carry raw password suggested_values."""

    _PW_SCHEMA: typing.ClassVar[list] = [
        {
            "name": "api_key",
            "selector": {"text": {"type": "password"}},
            "description": {"suggested_value": "sk-LIVEFLOWSECRET"},
        }
    ]

    @pytest.mark.asyncio
    async def test_raise_flow_api_error_redacts_current_step_schema(
        self, redact_on, monkeypatch
    ):
        from ha_mcp.client.rest_client import HomeAssistantAPIError
        from ha_mcp.tools import config_entry_flow_walker as walker

        monkeypatch.setattr(
            walker, "fetch_helper_flow_info", AsyncMock(return_value={})
        )
        with pytest.raises(ToolError) as exc_info:
            await walker._raise_flow_api_error(
                HomeAssistantAPIError("bad value", status_code=400),
                client=MagicMock(),
                flow_id="f1",
                helper_type="filter",
                menu_choice=None,
                current_step={"type": "form", "data_schema": self._PW_SCHEMA},
                submitted={"api_key": "x"},
            )
        body = json.loads(str(exc_info.value))
        schema = body["data_schema"]
        assert schema[0]["description"]["suggested_value"] == REDACTED_SET
        assert "sk-LIVEFLOWSECRET" in known_secret_values()

    @pytest.mark.asyncio
    async def test_flow_helper_error_context_redacts_schema(self, redact_on):
        from ha_mcp.tools.tools_config_helpers import _flow_helper_error_context

        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "f1",
                "step_id": "user",
                "data_schema": self._PW_SCHEMA,
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})
        ctx = await _flow_helper_error_context(client, "filter")
        assert ctx["data_schema"][0]["description"]["suggested_value"] == REDACTED_SET


class TestEmptyFormSchemaFailsClosed:
    @pytest.mark.asyncio
    async def test_empty_form_schema_treated_as_unreadable(self):
        from ha_mcp.tools.tools_integrations import IntegrationTools

        client = MagicMock()
        client.start_options_flow = AsyncMock(
            return_value={"flow_id": "f1", "type": "form", "data_schema": []}
        )
        client.abort_options_flow = AsyncMock()
        tools = IntegrationTools(client)
        entry = {
            "entry_id": "e1",
            "supports_options": True,
            "options": {"maybe_pw": "hunter2secret"},
        }
        warnings: list[str] = []
        await tools._redact_component_options("e1", entry, warnings)
        assert entry["options"] == {"maybe_pw": REDACTED_SET}
        assert len(warnings) == 1


class TestWriteEntryPointsRejectSentinels:
    """Every flow-write entry point calls the sentinel guard before any
    client traffic — not just create_config_entry."""

    @pytest.mark.asyncio
    async def test_update_config_entry_options_rejects_before_flow(self, redact_on):
        from ha_mcp.tools.config_entry_flow import update_config_entry_options

        client = AsyncMock()
        with pytest.raises(ToolError):
            await update_config_entry_options(client, "e1", {"api_key": REDACTED_SET})
        client.get_config_entry.assert_not_awaited()
        client.start_options_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_config_subentry_rejects_before_flow(self, redact_on):
        from ha_mcp.tools.config_entry_flow import set_config_subentry

        client = AsyncMock()
        with pytest.raises(ToolError):
            await set_config_subentry(client, "e1", "device", {"token": REDACTED_EMPTY})
        client.start_config_subentry_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_config_entry_rejects_before_flow(self, redact_on):
        from ha_mcp.tools.config_entry_flow import create_config_entry

        client = AsyncMock()
        with pytest.raises(ToolError):
            await create_config_entry(client, "group", {"pw": REDACTED_SET})
        client.start_config_flow.assert_not_awaited()


class TestComponentPathEndToEndRedaction:
    """Component-served reads through the real formatting paths (issue 2157)."""

    def _tools(self, monkeypatch):
        from ha_mcp.tools import tools_integrations as ti

        monkeypatch.setattr(ti, "get_logger_levels", AsyncMock(return_value={}))
        client = MagicMock()
        client.start_options_flow = AsyncMock(
            return_value={
                "flow_id": "f1",
                "type": "form",
                "data_schema": FLOW_SCHEMA,
            }
        )
        client.abort_options_flow = AsyncMock()
        return ti.IntegrationTools(client)

    @pytest.mark.asyncio
    async def test_single_entry_include_schema_redacts_options_and_schema(
        self, redact_on, monkeypatch
    ):
        tools = self._tools(monkeypatch)
        rows = [
            {
                "entry_id": "e1",
                "domain": "demo",
                "supports_options": True,
                "options": {"api_key": "sk-LIVEFLOWSECRET", "host": "1.2.3.4"},
            }
        ]
        resp = await tools._single_entry_from_component(
            "e1",
            rows,
            True,
            include_subentries=False,
            include_subentry_schema=False,
            subentry_type=None,
            subentry_id=None,
            show_advanced_options=False,
        )
        assert resp["entry"]["options"]["api_key"] == REDACTED_SET
        assert resp["entry"]["options"]["host"] == "1.2.3.4"
        schema = resp["options_schema"]["data_schema"]
        api_field = next(f for f in schema if f.get("name") == "api_key")
        assert api_field["description"]["suggested_value"] == REDACTED_SET

    @pytest.mark.asyncio
    async def test_list_page_redacts_options(self, redact_on, monkeypatch):
        tools = self._tools(monkeypatch)
        rows = [
            {
                "entry_id": "e1",
                "domain": "demo",
                "title": "Demo",
                "state": "loaded",
                "supports_options": True,
                "options": {"api_key": "sk-LIVEFLOWSECRET"},
            }
        ]
        resp = await tools._list_entries_from_component(
            rows, None, None, True, None, 50, 0
        )
        assert resp["entries"][0]["options"]["api_key"] == REDACTED_SET


class TestScrubObjKeyCollision:
    def test_colliding_scrubbed_keys_get_suffixes(self):
        obj = {"SECRETVALUE1": "a", "SECRETVALUE2": "b", "ok": "c"}
        out = scrub_obj(obj, ("SECRETVALUE1", "SECRETVALUE2"))
        assert out == {
            REDACTED_KNOWN: "a",
            f"{REDACTED_KNOWN}#2": "b",
            "ok": "c",
        }


class TestMiddlewareNonStringErrorArgs:
    @pytest.mark.asyncio
    async def test_dict_arg_scrubbed(self, redact_on):
        register_known_secret_values(["SECRETVALUE1"])
        middleware = RedactSecretsMiddleware()
        call_next = AsyncMock(side_effect=ValueError({"detail": "boom SECRETVALUE1"}))
        with pytest.raises(ValueError) as exc_info:
            await middleware.on_call_tool(MagicMock(), call_next)
        assert exc_info.value.args[0] == {"detail": f"boom {REDACTED_KNOWN}"}
