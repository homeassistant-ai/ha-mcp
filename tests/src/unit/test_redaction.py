"""Unit tests for schema-driven secret redaction (issue 2157, redaction.py)."""

import json
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
        assert {"sk-LIVEFLOWSECRET", "legacypw99", "tok-INNERSECRET"} <= (
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
        assert known_secret_values() == frozenset()


class TestKnownSecretRegistry:
    def test_short_values_never_registered(self):
        register_known_secret_values(["abc", "12345", "long-enough-secret"])
        assert known_secret_values() == frozenset({"long-enough-secret"})

    def test_non_strings_ignored(self):
        register_known_secret_values([None, 123456, "real-secret-value"])  # type: ignore[list-item]
        assert known_secret_values() == frozenset({"real-secret-value"})


class TestScrub:
    def test_scrub_text_replaces_all_occurrences(self):
        out = scrub_text("token=SECRETVALUE1 again SECRETVALUE1", {"SECRETVALUE1"})
        assert out == f"token={REDACTED_KNOWN} again {REDACTED_KNOWN}"

    def test_scrub_text_longest_first(self):
        # The longer secret contains the shorter one; scrubbing must not
        # leave fragments of the longer secret behind.
        out = scrub_text("x SECRETLONGER y SECRET z", {"SECRET", "SECRETLONGER"})
        assert out == f"x {REDACTED_KNOWN} y {REDACTED_KNOWN} z"

    def test_scrub_obj_walks_dicts_lists_and_keys(self):
        obj = {
            "note": "uses SECRETVALUE1 here",
            "SECRETVALUE1": "as key",
            "nested": [{"v": "SECRETVALUE1"}, 42, None],
        }
        out = scrub_obj(obj, {"SECRETVALUE1"})
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

    def test_toggle_off_is_noop(self, redact_off):
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
