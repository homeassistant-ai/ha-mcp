"""Integrations whose options flows Core runs, for the redaction tests.

``FORM``'s options flow shows the stored options on a form that marks its
secrets in every way Core serializes one: a password text selector (also
with ``multiple`` and inside a section), and a ``probatio.Secret`` key. The
other domains' options flows open on a menu, on a form without fields, or
fail.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import probatio
import pytest
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient

from ._mock_flow_integration import answer_translations, options_only_config_flow

FORM = "redact_form"
MENU = "redact_menu"
EMPTY = "redact_empty"
BROKEN = "redact_broken"

PIN_DEFAULT = "pin-DEFAULT99"


def _text(kind: TextSelectorType, *, multiple: bool = False) -> TextSelector:
    return TextSelector(TextSelectorConfig(type=kind, multiple=multiple))


FIELDS = probatio.Schema(
    {
        probatio.Optional("name"): TextSelector(),
        probatio.Optional("contact"): _text(TextSelectorType.EMAIL),
        probatio.Optional("api_key"): _text(TextSelectorType.PASSWORD),
        probatio.Optional(probatio.Secret("password")): str,
        probatio.Optional("pin_code", default=PIN_DEFAULT): _text(
            TextSelectorType.PASSWORD
        ),
        probatio.Optional("tokens"): _text(TextSelectorType.PASSWORD, multiple=True),
        probatio.Optional("advanced"): section(
            probatio.Schema(
                {probatio.Optional("inner_token"): _text(TextSelectorType.PASSWORD)}
            )
        ),
    }
)

#: What a ``FORM`` entry stores; every value but ``name`` is a secret.
STORED: dict[str, Any] = {
    "name": "Kitchen hub",
    "api_key": "sk-LIVEFLOWSECRET",
    "password": "stored-device-password",
    "tokens": ["tok-AAA111", ""],
    "advanced": {"inner_token": "tok-INNERSECRET"},
}
STORED_SECRETS = {
    "sk-LIVEFLOWSECRET",
    "stored-device-password",
    "tok-AAA111",
    "tok-INNERSECRET",
}


class _FormOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                FIELDS, self.config_entry.options
            ),
        )


class _MenuOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["details"])


class _EmptyOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="init")


class _BrokenOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        raise ConnectionError("the device did not answer")


@pytest.fixture
def redaction_flows(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The four integrations and their flows; Core reads their manifests."""
    monkeypatch.chdir(tmp_path)
    flows = {
        FORM: _FormOptions,
        MENU: _MenuOptions,
        EMPTY: _EmptyOptions,
        BROKEN: _BrokenOptions,
    }
    for domain in flows:
        mock_integration(hass, MockModule(domain))
        mock_platform(hass, f"{domain}.config_flow", None)
    with (
        mock_config_flow(FORM, options_only_config_flow(_FormOptions)),
        mock_config_flow(MENU, options_only_config_flow(_MenuOptions)),
        mock_config_flow(EMPTY, options_only_config_flow(_EmptyOptions)),
        mock_config_flow(BROKEN, options_only_config_flow(_BrokenOptions)),
    ):
        yield


@pytest.fixture
async def redaction_core(hass: HomeAssistant, redaction_flows: None) -> None:
    """Core's config API, log levels and flow strings."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "logger", {})
    answer_translations(hass)


def add_entry(
    hass: HomeAssistant, domain: str = FORM, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """An entry of ``domain`` storing ``options`` (default ``STORED``)."""
    entry = MockConfigEntry(
        domain=domain, title="Hub", options=STORED if options is None else options
    )
    entry.add_to_hass(hass)
    return entry


async def options_form(
    client: HomeAssistantClient, entry: MockConfigEntry
) -> dict[str, Any]:
    """The entry's options form as Core serializes it; the flow is closed."""
    step: dict[str, Any] = await client.start_options_flow(entry.entry_id)
    await client.abort_options_flow(step["flow_id"])
    return step


def field_named(fields: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(field for field in fields if field.get("name") == name)
