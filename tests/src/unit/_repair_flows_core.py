"""A test integration whose Repairs fix flows Core runs, for the
``ha_manage_updates(action='fix_repair')`` tests.

Its repairs platform hands Core's ``RepairsFlowManager`` one flow per issue
(Core's own ``ConfirmRepairFlow`` for ``CONFIRM``); the server's client
(``flow_client``: without the component, and with it, where Home Assistant
2026.12 formats the texts) drives them over Core's fix-flow endpoints, and
Core's ``frontend/get_translations`` reads their strings.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import voluptuous as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    FlowType,
    RepairsFlow,
    RepairsFlowResult,
    repairs_flow_manager,
)
from homeassistant.config_entries import SOURCE_USER, ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import mock_config_flow, mock_platform

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_updates import UpdateTools

from ._mock_flow_integration import answer_translations, mock_flow_integration

DOMAIN = "repair_probe"
# Core's ConfirmRepairFlow: one form without fields.
CONFIRM = "confirm_issue"
# A form asking for CODE; any other code is refused with a field error.
FORM = "form_issue"
# A menu: "replace" fixes the issue, "later" aborts.
MENU = "menu_issue"
# Aborted by the flow as it starts.
ABORT = "abort_issue"
# Aborted with a reason a reconfigure flow commits with.
SUCCESS_REASON_ABORT = "success_reason_abort_issue"
# Fixed by handing over to the config flow it opens (next_flow).
HANDOFF = "handoff_issue"
# Aborted, handing over to the config flow it opens.
HANDOFF_ABORT = "handoff_abort_issue"
# A progress step, then CONFIRM's form.
PROGRESS = "progress_issue"
# A confirm form whose step fills {device} itself, with STEP_DEVICE.
STEP_PLACEHOLDER = "step_placeholder_issue"
# Raised with is_fixable=False.
UNFIXABLE = "unfixable_issue"

CODE = "1234"
PLACEHOLDERS = {"device": "the kettle"}
STEP_DEVICE = "the spare kettle"
STRINGS = {
    "issues": {
        "kettle": {
            "title": "The kettle is broken",
            "fix_flow": {
                "step": {
                    "confirm": {
                        "title": "Replace {device}",
                        "description": "Select Submit once {device} is replaced.",
                    },
                    "code": {
                        "data": {"code": "Code"},
                        "data_description": {
                            "code": "The code printed on {device}, {length} long"
                        },
                        "submit": "Check",
                    },
                    "pick": {
                        "menu_options": {"replace": "Replace {device}"},
                        "menu_option_descriptions": {"replace": "Order a new one"},
                    },
                },
                "error": {"wrong_code": "That code does not fit {device}"},
                "abort": {"not_now": "{device} cannot be fixed now"},
                "create_entry": {"default": "The kettle works again."},
            },
        }
    }
}


class _FormFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_code()

    async def async_step_code(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["code"] == CODE:
                return self.async_create_entry(data={})
            errors["code"] = "wrong_code"
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
            description_placeholders={"length": "4 digits"},
        )


class _MenuFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_pick()

    async def async_step_pick(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return self.async_show_menu(step_id="pick", menu_options=["replace", "later"])

    async def async_step_replace(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return self.async_create_entry(data={})

    async def async_step_later(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return self.async_abort(reason="not_now")


class _AbortFlow(RepairsFlow):
    reason = "not_now"

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        # The frontend still reads a fix flow's abort text under the issue's
        # domain.
        return self.async_abort(reason=self.reason, translation_domain="homeassistant")


class _SuccessReasonAbortFlow(_AbortFlow):
    reason = "reconfigure_successful"


class _HandoffFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        setup = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        return self.async_create_entry(
            data={}, next_flow=(FlowType.CONFIG_FLOW, setup["flow_id"])
        )


class _HandoffAbortFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        setup = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        return self.async_abort(
            reason="moved", next_flow=(FlowType.CONFIG_FLOW, setup["flow_id"])
        )


class _ProgressFlow(ConfirmRepairFlow):
    task: asyncio.Task[None] | None = None

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if self.task is None:
            self.task = self.hass.async_create_task(asyncio.sleep(0))
        if not self.task.done():
            return self.async_show_progress(
                step_id="init", progress_action="draining", progress_task=self.task
            )
        return self.async_show_progress_done(next_step_id="confirm")


class _StepPlaceholderFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"device": STEP_DEVICE},
        )


class _SetupFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="user")


_FLOWS: dict[str, type[RepairsFlow]] = {
    CONFIRM: ConfirmRepairFlow,
    FORM: _FormFlow,
    MENU: _MenuFlow,
    ABORT: _AbortFlow,
    SUCCESS_REASON_ABORT: _SuccessReasonAbortFlow,
    HANDOFF: _HandoffFlow,
    HANDOFF_ABORT: _HandoffAbortFlow,
    PROGRESS: _ProgressFlow,
    STEP_PLACEHOLDER: _StepPlaceholderFlow,
}


async def _create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return _FLOWS[issue_id]()


def open_fix_flows(hass: HomeAssistant) -> dict[str, str | None]:
    """The fix flows open in Core, flow_id -> the step each is at."""
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return {flow["flow_id"]: flow.get("step_id") for flow in manager.async_progress()}


def issue_exists(hass: HomeAssistant, issue_id: str) -> bool:
    """Whether Core's issue registry still holds the test integration's issue."""
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


def raise_issue(hass: HomeAssistant, issue_id: str, *, fixable: bool = True) -> None:
    """File ``issue_id`` of the test integration in Core's issue registry."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=fixable,
        severity=ir.IssueSeverity.WARNING,
        translation_key="kettle",
        translation_placeholders=PLACEHOLDERS,
    )


@pytest.fixture
async def fix_tools(
    hass: HomeAssistant,
    flow_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[UpdateTools]:
    """``ha_manage_updates`` on a ``hass`` with Repairs and the test
    integration loaded, every issue of ``_FLOWS`` and ``UNFIXABLE`` filed."""
    answer_translations(hass)
    mock_flow_integration(hass, DOMAIN, tmp_path, monkeypatch, STRINGS)
    mock_platform(
        hass, f"{DOMAIN}.repairs", Mock(async_create_fix_flow=_create_fix_flow)
    )
    assert await async_setup_component(hass, "repairs", {})
    assert await async_setup_component(hass, DOMAIN, {})
    for issue_id in _FLOWS:
        raise_issue(hass, issue_id)
    raise_issue(hass, UNFIXABLE, fixable=False)
    with mock_config_flow(DOMAIN, _SetupFlow):
        yield UpdateTools(flow_client)
