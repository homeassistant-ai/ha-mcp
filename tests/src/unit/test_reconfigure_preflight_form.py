"""The reconfigure preview reads the integration's own reconfigure form (#2632).

The preview leg of ``ha_set_integration(reconfigure=True)`` starts the
official reconfigure flow, describes its first step (fields, current values,
Core's help text) and aborts it. Config keys that form does not declare are
warned about, since a later form may declare them, and refused when the form
is the flow's last. A flow Core aborts at once refuses the preview with Core's
reason text; one Core finishes at once is reported as possibly applied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import integration_reconfigure
from ha_mcp.tools.config_entry_identity import ReconfigureIdentity
from ha_mcp.tools.config_entry_reconfigure import PreparedReconfigure
from ha_mcp.tools.integration_reconfigure import ReconfigureRunner
from ha_mcp.tools.reconfigure_preflight import _declared_keys, read_reconfigure_form

_HOST = {
    "name": "host",
    "required": True,
    "selector": {"text": {}},
    "description": {"suggested_value": "10.0.0.2"},
}
_ADVANCED = {
    "name": "advanced",
    "type": "expandable",
    "schema": [{"name": "port", "selector": {"number": {}}}],
}


def _form(**extra: Any) -> dict[str, Any]:
    return {
        "type": "form",
        "flow_id": "f1",
        "step_id": "reconfigure",
        "data_schema": [_HOST, _ADVANCED],
        **extra,
    }


def _client(first_step: dict[str, Any], strings: dict[str, str] | None = None):
    client = AsyncMock()
    client.start_reconfigure_flow.return_value = first_step
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"resources": strings or {}},
    }
    return client


async def _read(client: Any, config: dict[str, Any]):
    return await read_reconfigure_form(
        client, entry_id="e1", domain="demo", config=config
    )


@pytest.mark.asyncio
async def test_preview_shows_the_reconfigure_form_and_aborts_its_flow() -> None:
    client = _client(
        _form(last_step=True),
        {"component.demo.config.step.reconfigure.data_description.host": "IP."},
    )

    form, warnings = await _read(client, {"host": "10.0.0.5"})

    assert form["step_id"] == "reconfigure"
    host = next(f for f in form["fields"] if f["name"] == "host")
    assert host["current"] == "10.0.0.2"
    assert host["description"] == "IP."
    assert warnings == []
    client.start_reconfigure_flow.assert_awaited_once_with("demo", "e1")
    client.submit_config_flow_step.assert_not_called()
    client.abort_config_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_keys_the_first_form_does_not_declare_are_warned_about() -> None:
    client = _client(_form(last_step=False))

    _, warnings = await _read(
        client,
        {
            "host": "10.0.0.5",
            "port": 81,
            "advanced": {"port": 81},
            "next_step_id": "x",
            "step_values": {"other": {"a": 1}},
            "api_token": "t",
        },
    )

    (warning,) = warnings
    assert "api_token" in warning
    for declared in ("host", "port", "advanced", "next_step_id", "step_values"):
        assert f"'{declared}'" not in warning


@pytest.mark.asyncio
async def test_keys_the_last_form_does_not_declare_are_refused() -> None:
    client = _client(_form(last_step=True))

    with pytest.raises(ToolError) as exc_info:
        await _read(client, {"host": "10.0.0.5", "hostname": "x"})

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["unknown_config_keys"] == ["hostname"]
    assert body["status"] == "flow_aborted_before_apply"
    client.abort_config_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_a_flow_core_aborts_at_once_refuses_the_preview_with_its_text() -> None:
    client = _client(
        {
            "type": "abort",
            "flow_id": "f1",
            "reason": "not_supported",
            "description_placeholders": {"model": "X1"},
        },
        {"component.demo.config.abort.not_supported": "{model} cannot be changed."},
    )

    with pytest.raises(ToolError) as exc_info:
        await _read(client, {"host": "10.0.0.5"})

    context = json.loads(str(exc_info.value))
    assert context["reason"] == "not_supported"
    assert context["reason_text"] == "X1 cannot be changed."
    assert context["status"] == "flow_aborted_before_apply"
    client.abort_config_flow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["reconfigure_successful", "reauth_successful"])
async def test_a_flow_core_finishes_at_once_is_reported_as_applied(
    reason: str,
) -> None:
    """Core aborts with a success reason after it updated the entry."""
    client = _client({"type": "abort", "flow_id": "f1", "reason": reason})

    with pytest.raises(ToolError) as exc_info:
        await _read(client, {"host": "10.0.0.5"})

    context = json.loads(str(exc_info.value))
    assert context["status"] == "applied_but_unverified"
    assert context["reason"] == reason


def test_a_section_without_a_name_declares_no_key() -> None:
    schema = [_HOST, {"type": "expandable", "schema": [{"name": "port"}]}]

    assert _declared_keys(schema) == {"host", "port"}


@pytest.mark.asyncio
async def test_a_menu_first_flow_lists_its_options_without_a_key_check() -> None:
    client = _client(
        {
            "type": "menu",
            "flow_id": "f1",
            "step_id": "reconfigure",
            "menu_options": ["usb", "network"],
        }
    )

    form, warnings = await _read(client, {"anything": 1})

    assert form["menu_options"] == ["usb", "network"]
    assert warnings == []
    client.abort_config_flow.assert_awaited_once_with("f1")


def _prepared(monkeypatch: pytest.MonkeyPatch) -> PreparedReconfigure:
    prepared = PreparedReconfigure(
        entry_id="e1",
        entry={"entry_id": "e1", "domain": "demo", "title": "Demo"},
        flow_config={"host": "10.0.0.5", "api_token": "t"},
        identity=ReconfigureIdentity(
            unique_id=None, unique_id_known=False, device_ids=["d1"]
        ),
        expected_identity={},
    )
    monkeypatch.setattr(
        integration_reconfigure,
        "prepare_reconfigure_request",
        AsyncMock(return_value=prepared),
    )
    return prepared


@pytest.mark.asyncio
async def test_only_the_preview_leg_reads_the_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(monkeypatch)
    client = _client(_form(last_step=False))
    runner = ReconfigureRunner(client)

    preview = await runner.run("e1", config=prepared.flow_config)

    assert preview["reconfigure_form"]["fields"][0]["name"] == "host"
    assert any("api_token" in w for w in preview["warnings"])
    client.start_reconfigure_flow.reset_mock()
    with pytest.raises(ToolError):
        await runner.run("e1", config=prepared.flow_config, confirm_token="stale")
    client.start_reconfigure_flow.assert_not_called()


@pytest.mark.asyncio
async def test_a_password_fields_live_value_does_not_leak_into_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True, redact_app_option_keys=""),
    )
    api_key = {
        "name": "api_key",
        "required": True,
        "selector": {"text": {"type": "password"}},
        "description": {"suggested_value": "sk-LIVE"},
    }
    client = _client({**_form(), "data_schema": [_HOST, api_key]})

    form, _ = await _read(client, {"host": "10.0.0.5"})

    assert "sk-LIVE" not in json.dumps(form)
    assert any(f["name"] == "api_key" for f in form["fields"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_step", "keys_checked"),
    [
        (_form(last_step=False), True),
        (
            {"type": "menu", "flow_id": "f1", "step_id": "reconfigure"},
            False,
        ),
    ],
)
async def test_the_preview_message_claims_a_key_check_only_when_one_ran(
    monkeypatch: pytest.MonkeyPatch, first_step: dict[str, Any], keys_checked: bool
) -> None:
    prepared = _prepared(monkeypatch)

    preview = await ReconfigureRunner(_client(first_step)).run(
        "e1", config=prepared.flow_config
    )

    assert ("config keys" in preview["message"]) is keys_checked
