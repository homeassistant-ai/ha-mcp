"""Options-flow forms under redact_secrets, as Core serializes them (#2157).

Core shows the stored options in the form's ``suggested_value``s, and only
the form marks which fields are secret. The forms are test integrations'
options flows, run by Core and read through the server's own client, as
``ha_get_integration`` and ``ha_set_integration`` read them.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.redaction import (
    REDACTED_EMPTY,
    REDACTED_SET,
    is_password_flow_field,
    known_secret_values,
    redact_flow_schema,
    redact_options_by_flow_schema,
)
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._redaction_forms_core import (
    BROKEN,
    EMPTY,
    MENU,
    PIN_DEFAULT,
    STORED,
    STORED_SECRETS,
    add_entry,
    field_named,
    options_form,
)
from ._redaction_forms_core import redaction_core as redaction_core
from ._redaction_forms_core import redaction_flows as redaction_flows
from ._redaction_registry import clean_registry as clean_registry
from ._tool_errors import error_body

#: STORED as an options form redacts it: every secret field, nested ones too.
REDACTED_STORED = {
    "name": "Kitchen hub",
    "api_key": REDACTED_SET,
    "password": REDACTED_SET,
    "tokens": [REDACTED_SET, REDACTED_EMPTY],
    "advanced": {"inner_token": REDACTED_SET},
}


@pytest.fixture
def redact_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_global_settings(), "redact_secrets", False)


@pytest.fixture
async def client(
    redaction_core: None, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return core_client


@pytest.fixture
async def form(hass: HomeAssistant, client: HomeAssistantClient) -> list[Any]:
    """The fields of a stored entry's options form, as Core serializes them."""
    step = await options_form(client, add_entry(hass))
    return cast("list[Any]", step["data_schema"])


async def _get(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    return cast(
        "dict[str, Any]", await IntegrationTools(client).ha_get_integration(**kwargs)
    )


def _inner(fields: list[Any], section: str, name: str) -> dict[str, Any]:
    return field_named(field_named(fields, section)["schema"], name)


# --- which fields a form marks as secret ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "secret"),
    [("api_key", True), ("password", True), ("name", False), ("contact", False)],
    ids=["password-selector", "core-secret-key", "plain-text", "email-text"],
)
async def test_a_field_is_secret_by_its_password_selector_or_cores_secret_flag(
    form: list[Any], name: str, secret: bool
) -> None:
    assert is_password_flow_field(field_named(form, name)) is secret


# --- the form handed back to a caller --------------------------------------------------


async def test_a_redacted_form_shows_no_stored_secret_or_secret_default(
    form: list[Any],
) -> None:
    out = redact_flow_schema(form)

    shown = {
        name: field_named(out, name)["description"]["suggested_value"]
        for name in ("api_key", "password", "tokens")
    }
    assert shown == {
        "api_key": REDACTED_SET,
        "password": REDACTED_SET,
        "tokens": [REDACTED_SET, REDACTED_EMPTY],
    }
    assert field_named(out, "pin_code")["default"] == REDACTED_SET
    inner = _inner(out, "advanced", "inner_token")
    assert inner["description"]["suggested_value"] == REDACTED_SET


async def test_a_redacted_form_keeps_ordinary_values_and_the_original_form(
    form: list[Any],
) -> None:
    before = json.dumps(form, sort_keys=True)

    out = redact_flow_schema(form)

    assert field_named(out, "name")["description"]["suggested_value"] == "Kitchen hub"
    assert json.dumps(form, sort_keys=True) == before


async def test_the_secrets_a_form_showed_are_scrubbed_from_every_later_response(
    form: list[Any],
) -> None:
    redact_flow_schema(form)

    assert {*STORED_SECRETS, PIN_DEFAULT} <= set(known_secret_values())


# --- stored options redacted by their form ---------------------------------------------


async def test_stored_options_hide_every_field_the_form_marks_secret(
    form: list[Any],
) -> None:
    out = redact_options_by_flow_schema({**STORED, "extra": "kept"}, form)

    assert out == {**REDACTED_STORED, "extra": "kept"}
    assert set(known_secret_values()) >= STORED_SECRETS


async def test_a_secret_stored_before_its_field_moved_into_a_section_is_hidden(
    form: list[Any],
) -> None:
    """Core keeps stored options as they were saved, so an entry saved before
    an update moved ``inner_token`` into ``advanced`` still holds it at the
    top."""
    out = redact_options_by_flow_schema({"inner_token": "tok-OLDPLACE"}, form)

    assert out == {"inner_token": REDACTED_SET}


async def test_redacting_redacted_options_again_changes_nothing(
    form: list[Any],
) -> None:
    once = redact_options_by_flow_schema(STORED, form)
    assert redact_options_by_flow_schema(once, form) == once


# --- ha_get_integration without the component: options read from the form ------------


async def test_options_read_from_the_form_hide_the_secret_fields(
    hass: HomeAssistant, client: HomeAssistantClient, redact_on: None
) -> None:
    entry = add_entry(hass)

    resp = await _get(client, entry_id=entry.entry_id, include_schema=True)

    assert resp["entry"]["options"] == {
        "name": "Kitchen hub",
        "api_key": REDACTED_SET,
        "password": REDACTED_SET,
        "pin_code": REDACTED_SET,
        "tokens": [REDACTED_SET, REDACTED_EMPTY],
        "inner_token": REDACTED_SET,
    }
    assert {*STORED_SECRETS, PIN_DEFAULT} <= set(known_secret_values())


async def test_with_redaction_off_options_read_from_the_form_are_the_stored_values(
    hass: HomeAssistant, client: HomeAssistantClient, redact_off: None
) -> None:
    entry = add_entry(hass)

    resp = await _get(client, entry_id=entry.entry_id, include_schema=True)

    options = resp["entry"]["options"]
    assert (options["api_key"], options["pin_code"]) == (
        "sk-LIVEFLOWSECRET",
        PIN_DEFAULT,
    )
    assert known_secret_values() == ()


# --- ha_get_integration through the component: stored options and the form ----------


@pytest.fixture
async def component_client(
    client: HomeAssistantClient, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return component_core_client


async def test_a_schema_read_hides_the_secrets_in_the_options_and_the_form(
    hass: HomeAssistant, component_client: HomeAssistantClient, redact_on: None
) -> None:
    entry = add_entry(hass)

    resp = await _get(component_client, entry_id=entry.entry_id, include_schema=True)

    assert resp["entry"]["options"] == REDACTED_STORED
    fields = resp["options_schema"]["fields"]
    assert field_named(fields, "api_key")["current"] == REDACTED_SET
    assert field_named(fields, "name")["current"] == "Kitchen hub"
    advanced = field_named(fields, "advanced")["fields"]
    assert field_named(advanced, "inner_token")["current"] == REDACTED_SET
    assert "warnings" not in resp


_UNREADABLE = "redact_secrets: no options schema readable"


@pytest.mark.parametrize("domain", [MENU, EMPTY, BROKEN])
async def test_without_a_readable_form_every_option_is_withheld_with_a_warning(
    hass: HomeAssistant,
    component_client: HomeAssistantClient,
    redact_on: None,
    domain: str,
) -> None:
    """A menu, a form without fields or a failed flow marks no field, so no
    stored value can be told safe."""
    entry = add_entry(
        hass,
        domain,
        {"name": "Kitchen hub", "maybe_pw": "hunter2secret", "empty": ""},
    )

    resp = await _get(component_client, entry_id=entry.entry_id, include_schema=True)

    assert resp["entry"]["options"] == {
        "name": REDACTED_SET,
        "maybe_pw": REDACTED_SET,
        "empty": REDACTED_EMPTY,
    }
    assert [w for w in resp["warnings"] if w.startswith(_UNREADABLE)] == [
        f"{_UNREADABLE} for {entry.entry_id} — every option value was redacted "
        "conservatively (the password fields cannot be told apart without the "
        "schema)"
    ]


async def test_an_entry_without_options_needs_no_withheld_warning(
    hass: HomeAssistant, component_client: HomeAssistantClient, redact_on: None
) -> None:
    entry = add_entry(hass, BROKEN, {})

    resp = await _get(component_client, entry_id=entry.entry_id, include_schema=True)

    assert resp["entry"]["options"] == {}
    assert not [w for w in resp.get("warnings", []) if w.startswith(_UNREADABLE)]


# --- a rejected options submit -----------------------------------------------------


@pytest.mark.parametrize("redact", [True, False], ids=["on", "off"])
async def test_a_rejected_options_submit_shows_the_form_without_its_secrets(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    redact: bool,
) -> None:
    """Core refuses a value of the wrong type with a 400; the error carries
    the form so the caller sees what it takes."""
    monkeypatch.setattr(get_global_settings(), "redact_secrets", redact)
    entry = add_entry(hass)

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id=entry.entry_id, config={"name": 12345}
        )

    body = error_body(exc_info)
    assert body["status_code"] == 400
    api_key = field_named(body["data_schema"], "api_key")
    shown = api_key["description"]["suggested_value"]
    assert shown == (REDACTED_SET if redact else "sk-LIVEFLOWSECRET")
    assert ("sk-LIVEFLOWSECRET" in known_secret_values()) is redact
