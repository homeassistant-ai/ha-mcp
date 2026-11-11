"""fetch_helper_flow_info: one-round-trip introspection of a helper's config flow.

Split out of test_helper_schema_inline.py (issue #1186 tests). A config flow of
the test's own integration is registered in a real ``hass``; the helper reads
it through ``core_client``, so every flow step and rejection is Core's own.
"""

from __future__ import annotations

from collections.abc import Container
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
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
from ha_mcp.tools.config_entry_flow_introspect import fetch_helper_flow_info

DOMAIN = "helper_flow_info_probe"

_ENTITY_SCHEMA = probatio.Schema({probatio.Required("entity_id"): str})
_STATE_SCHEMA = probatio.Schema({probatio.Required("state"): str})

# The ids of the flows Core started, in order.
started: list[str] = []


def _serialized(schema: probatio.Schema) -> Any:
    """The schema as Core's flow view sends it."""
    return probatio.to_field_list(schema, custom_serializer=cv.custom_serializer)


class _FormFlow(ConfigFlow):
    """A non-menu helper (``filter``): the first step is a form."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        started.append(self.flow_id)
        return self.async_show_form(step_id="user", data_schema=_ENTITY_SCHEMA)


def _menu_flow(options: Container[str]) -> type[ConfigFlow]:
    """A menu-rooted helper (``template``) offering ``options``."""

    class _MenuFlow(ConfigFlow):
        VERSION = 1

        async def async_step_user(
            self, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            started.append(self.flow_id)
            return self.async_show_menu(step_id="user", menu_options=options)

        async def async_step_sensor(
            self, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            return self.async_show_form(step_id="sensor", data_schema=_STATE_SCHEMA)

    return _MenuFlow


@pytest.fixture
async def helper(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` that knows the test's integration."""
    assert await async_setup_component(hass, "config", {})
    mock_integration(hass, MockModule(DOMAIN))
    mock_platform(hass, f"{DOMAIN}.config_flow")
    started.clear()
    return core_client


class TestFetchHelperFlowInfo:
    """``fetch_helper_flow_info`` introspects a helper's config flow in
    a single HA round-trip and returns a dict with optional ``"schema"``
    and ``"menu_options"`` keys. Replaces the prior two helpers
    (``_fetch_data_schema_for_error_context`` + ``fetch_helper_menu_options``)
    that did the same flow start twice for menu-rooted helpers without
    a branch picked.
    """

    async def test_form_flow_returns_schema(
        self, hass: HomeAssistant, helper: HomeAssistantClient
    ) -> None:
        # ``filter`` is non-menu — top step is a form whose data_schema
        # is returned directly.
        with (
            mock_config_flow(DOMAIN, _FormFlow),
            patch.object(
                helper, "abort_config_flow", wraps=helper.abort_config_flow
            ) as abort,
        ):
            info = await fetch_helper_flow_info(helper, DOMAIN)

        assert info == {"schema": _serialized(_ENTITY_SCHEMA)}
        abort.assert_called_once_with(started[0])
        assert hass.config_entries.flow.async_progress() == []

    async def test_menu_flow_with_choice_submits_and_returns_branch_schema(
        self, helper: HomeAssistantClient
    ) -> None:
        # ``template`` with ``menu_choice="sensor"`` submits the menu
        # selection and returns the sensor-branch form schema.
        with mock_config_flow(DOMAIN, _menu_flow(["sensor", "binary_sensor"])):
            info = await fetch_helper_flow_info(helper, DOMAIN, menu_choice="sensor")

        # menu_options is intentionally NOT surfaced when a choice was
        # picked — the caller already has it.
        assert info == {"schema": _serialized(_STATE_SCHEMA)}

    async def test_menu_flow_without_choice_returns_menu_options(
        self, helper: HomeAssistantClient
    ) -> None:
        # ``template`` without a menu_choice can't be schema-fetched —
        # surface the legal sub-types instead so the caller can pick a
        # branch on the next try.
        with (
            mock_config_flow(DOMAIN, _menu_flow(["sensor", "binary_sensor", "button"])),
            patch.object(
                helper,
                "submit_config_flow_step",
                wraps=helper.submit_config_flow_step,
            ) as submit,
        ):
            info = await fetch_helper_flow_info(helper, DOMAIN)

        assert info == {"menu_options": ["sensor", "binary_sensor", "button"]}
        # No submit_config_flow_step call — single HA round-trip.
        submit.assert_not_called()

    async def test_returns_empty_on_ha_failure(
        self, helper: HomeAssistantClient
    ) -> None:
        with patch.object(
            helper,
            "start_config_flow",
            AsyncMock(side_effect=HomeAssistantConnectionError("offline")),
        ):
            info = await fetch_helper_flow_info(helper, "template")

        assert info == {}

    async def test_filters_non_string_menu_options(
        self, helper: HomeAssistantClient
    ) -> None:
        # Defensive — if HA returns a non-string entry, drop it rather
        # than propagating type confusion to the caller. Core passes an
        # integration's menu options through as the flow gave them.
        options: Any = ["sensor", 42, None, "binary_sensor"]
        with mock_config_flow(DOMAIN, _menu_flow(options)):
            info = await fetch_helper_flow_info(helper, DOMAIN)

        assert info == {"menu_options": ["sensor", "binary_sensor"]}

    async def test_menu_options_absent_when_not_a_list(
        self, helper: HomeAssistantClient
    ) -> None:
        # A menu whose ``menu_options`` is not a list (a string is a valid
        # ``Container[str]`` for Core) yields ``{}`` rather than a broken
        # ``{"menu_options": "sensor"}`` shape.
        with mock_config_flow(DOMAIN, _menu_flow("sensor")):
            info = await fetch_helper_flow_info(helper, DOMAIN)

        assert info == {}

    async def test_menu_options_absent_when_list_is_empty(
        self, helper: HomeAssistantClient
    ) -> None:
        # An empty options list still drops the ``menu_options`` key so
        # callers don't have to special-case empty-list-vs-missing.
        with mock_config_flow(DOMAIN, _menu_flow([])):
            info = await fetch_helper_flow_info(helper, DOMAIN)

        assert info == {}

    async def test_submit_failure_keeps_empty_info(
        self, hass: HomeAssistant, helper: HomeAssistantClient
    ) -> None:
        # If Core rejects the menu choice, the helper returns ``{}``
        # rather than a partially-populated dict, and still aborts the flow.
        with mock_config_flow(DOMAIN, _menu_flow(["sensor"])):
            info = await fetch_helper_flow_info(
                helper, DOMAIN, menu_choice="binary_sensor"
            )

        assert info == {}
        assert hass.config_entries.flow.async_progress() == []
