"""Home Assistant's own flow strings, as the frontend reads them.

The reads go through ``core_client`` to a real ``hass`` that answers
``frontend/get_translations`` with Core's own handler, so every string and
error reply is Core's.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_helpers.describe import describe_form
from ha_mcp.tools.flow_translations import fetch_flow_strings, flow_text

_ABORT = "component.sun.config.abort.single_instance_allowed"
_NOT_SUPPORTED = "component.demo.config.abort.not_supported"


_STRINGS = {
    _ABORT: "Only one.",
    "component.demo.options.error.cannot_connect": "Cannot reach {host}:{port}.",
    _NOT_SUPPORTED: "{model} is not supported by {integration}.",
}


@pytest.mark.parametrize(
    ("domain", "key", "placeholders", "expected"),
    [
        ("sun", "config.abort.single_instance_allowed", None, "Only one."),
        (
            "demo",
            "options.error.cannot_connect",
            {"host": "10.0.0.2", "port": "8080"},
            "Cannot reach 10.0.0.2:8080.",
        ),
        ("sun", "config.abort.unknown", None, None),
        ("demo", "config.abort.single_instance_allowed", None, None),  # other domain
        (
            "demo",
            "config.abort.not_supported",
            {"model": "X1"},
            "X1 is not supported by {integration}.",
        ),
    ],
)
def test_flow_text(
    domain: str, key: str, placeholders: dict[str, str] | None, expected: str | None
) -> None:
    assert flow_text(_STRINGS, domain, key, placeholders) == expected


@pytest.fixture
def translations(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` that answers ``frontend/get_translations``.

    The frontend integration cannot be set up here (its web assets are not
    installed), so only Core's own translations command is registered.
    """
    websocket_api.async_register_command(hass, websocket_get_translations)
    return core_client


@pytest.mark.asyncio
async def test_fetch_flow_strings_sends_one_english_request(
    hass: HomeAssistant, translations: HomeAssistantClient
) -> None:
    with patch.object(
        translations,
        "send_websocket_message",
        wraps=translations.send_websocket_message,
    ) as sent:
        strings = await fetch_flow_strings(translations, "sun", "config")

    assert strings
    assert strings == await async_get_translations(hass, "en", "config", ["sun"])
    sent.assert_awaited_once_with(
        {
            "type": "frontend/get_translations",
            "language": "en",
            "category": "config",
            "integration": ["sun"],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connected",
    [
        pytest.param(False, id="call-fails"),
        pytest.param(True, id="core-error"),
    ],
)
async def test_fetch_flow_strings_is_none_when_unreadable(
    core_client: HomeAssistantClient, connected: bool
) -> None:
    # None, not {}: a failed read must stay distinguishable from no strings.
    # Connected, Core answers unknown_command: nothing registered the command.
    no_answer = (
        contextlib.nullcontext()
        if connected
        else patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("ws down")),
        )
    )
    with no_answer:
        assert await fetch_flow_strings(core_client, "sun", "config") is None


@pytest.mark.asyncio
async def test_fetch_flow_strings_is_empty_when_core_has_no_strings(
    hass: HomeAssistant, translations: HomeAssistantClient
) -> None:
    # An integration that ships no translations.
    mock_integration(hass, MockModule("flow_strings_none"))

    assert await fetch_flow_strings(translations, "flow_strings_none", "config") == {}


_SECTIONS_DOMAIN = "flow_strings_sections"


class _SectionsFlow(ConfigFlow):
    """A form with a top-level ``port`` and a ``port`` in two sections."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        port = {probatio.Required("port"): int}
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    **port,
                    probatio.Required("a"): section(probatio.Schema(port)),
                    probatio.Required("b"): section(probatio.Schema(port)),
                }
            ),
        )


@pytest.mark.asyncio
async def test_same_named_fields_in_sections_keep_their_own_help(
    hass: HomeAssistant,
    translations: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Core reads a mocked integration's translations from its relative
    # package path, so the integration's files live under the test's own cwd.
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "homeassistant" / "components" / _SECTIONS_DOMAIN
    (folder / "translations").mkdir(parents=True)
    (folder / "translations" / "en.json").write_text(
        json.dumps(
            {
                "config": {
                    "step": {
                        "user": {
                            "data_description": {"port": "Top-level port"},
                            "sections": {
                                "a": {"data_description": {"port": "Port in a"}},
                                "b": {"data_description": {"port": "Port in b"}},
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert await async_setup_component(hass, "config", {})
    mock_integration(
        hass, MockModule(_SECTIONS_DOMAIN), top_level_files={"translations"}
    )
    mock_platform(hass, f"{_SECTIONS_DOMAIN}.config_flow")

    with mock_config_flow(_SECTIONS_DOMAIN, _SectionsFlow):
        step = await translations.start_config_flow(_SECTIONS_DOMAIN)
        try:
            fields = (
                await describe_form(translations, _SECTIONS_DOMAIN, "config", step)
            )["fields"]
        finally:
            await translations.abort_config_flow(step["flow_id"])

    assert fields[0]["description"] == "Top-level port"
    assert fields[1]["fields"][0]["description"] == "Port in a"
    assert fields[2]["fields"][0]["description"] == "Port in b"
