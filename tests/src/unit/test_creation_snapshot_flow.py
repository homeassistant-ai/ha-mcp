"""Complete recreation keeps native submission knowledge and every snapshot field.

Another integration's creation flow runs in a real Core and answers as the
test's scenario says; the server's client drives it over Core's REST API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_entry_flow import CreationFlowError, recreate_flow_helper

DOMAIN = "recreate_probe"

_FORM = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("state"): str,
        vol.Required("additional_options"): section(
            vol.Schema({vol.Optional("availability"): str})
        ),
    }
)


@dataclass
class _Run:
    """What the probe's flow does in one test, and what it received."""

    scenario: str = "complete"
    submissions: list[dict[str, Any]] = field(default_factory=list)
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    async def hold(self) -> None:
        """Keep the step waiting until the test lets it go."""
        self.reached.set()
        try:
            await self.release.wait()
        finally:
            self.done.set()


class _ProbeConfigFlow(ConfigFlow):
    """A menu-rooted creation flow: a menu, one form, then the entry."""

    VERSION = 1
    run: ClassVar[_Run] = _Run()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.run.scenario
        if scenario == "initial_create":
            return self.async_create_entry(title="Example", data={})
        if scenario == "initial_unknown":
            return self.async_external_step(
                step_id="private_step", url="https://private.invalid/"
            )
        if scenario == "cancel_start":
            await self.run.hold()
        return self.async_show_menu(step_id="user", menu_options=["sensor"])

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.run.scenario
        if user_input is None:
            if scenario == "menu_create":
                return self.async_create_entry(title="Example", data={})
            return self.async_show_form(
                step_id="sensor", data_schema=_FORM, last_step=True
            )
        self.run.submissions.append(user_input)
        if scenario == "form_followup":
            return self.async_show_form(
                step_id="sensor", data_schema=_FORM, last_step=True
            )
        if scenario == "menu_followup":
            return self.async_show_menu(
                step_id="private_menu", menu_options=["private_step"]
            )
        if scenario == "form_validation":
            return self.async_show_form(
                step_id="sensor",
                data_schema=_FORM,
                last_step=True,
                errors={
                    "additional_options.availability": "private-value",
                    "private_key.state": "private-value",
                },
            )
        if scenario == "server_error":
            # Core answers an exception in a step with HTTP 500.
            raise RuntimeError("step failed")
        if scenario == "cancel_submit":
            await self.run.hold()
            return self.async_abort(reason="released")
        return self.async_create_entry(title=user_input["name"], data={})

    async def async_step_private_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="not_reached")

    async def async_step_private_step(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="not_reached")


async def _set_up_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


@pytest.fixture
async def run(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Run]:
    probe = _Run()
    monkeypatch.setattr(_ProbeConfigFlow, "run", probe)
    mock_integration(hass, MockModule(DOMAIN, async_setup_entry=_set_up_entry))
    mock_platform(hass, f"{DOMAIN}.config_flow", None)
    with mock_config_flow(DOMAIN, _ProbeConfigFlow):
        assert await async_setup_component(hass, "config", {})
        yield probe


def _config() -> dict[str, Any]:
    return {
        "name": "Example",
        "template_type": "sensor",
        "state": "{{ 12 }}",
        "additional_options": {"availability": "{{ false }}"},
    }


async def test_complete_recreation_submits_nested_options_unchanged(
    hass: HomeAssistant, core_client: HomeAssistantClient, run: _Run
) -> None:
    client = core_client
    with (
        patch.object(
            client, "submit_config_flow_step", wraps=client.submit_config_flow_step
        ) as submit,
        patch.object(
            client, "abort_config_flow", wraps=client.abort_config_flow
        ) as abort,
    ):
        result = await recreate_flow_helper(client, DOMAIN, _config())
    [entry] = hass.config_entries.async_entries(DOMAIN)
    assert result["entry_id"] == entry.entry_id
    payload = deepcopy(_config())
    payload.pop("template_type")
    flow_id = submit.await_args_list[0].args[0]
    assert submit.await_args_list[1].args == (flow_id, payload)
    assert run.submissions == [payload]
    abort.assert_not_awaited()


@pytest.mark.parametrize(
    ("scenario", "status", "reason", "aborted"),
    [
        ("initial_create", "applied", "unsupported_form", False),
        ("menu_create", "applied", "unsupported_form", False),
        ("initial_unknown", "unknown", None, False),
        ("form_followup", "not_applied", "unsupported_form", True),
        ("menu_followup", "not_applied", "unsupported_form", True),
        ("form_validation", "not_applied", "validation_failed", True),
        ("server_error", "unknown", None, False),
    ],
)
async def test_complete_recreation_reports_native_outcomes_without_retry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    run: _Run,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    status: str,
    reason: str | None,
    aborted: bool,
) -> None:
    run.scenario = scenario
    client = core_client

    with (
        patch.object(
            client, "submit_config_flow_step", wraps=client.submit_config_flow_step
        ) as submit,
        patch.object(
            client, "abort_config_flow", wraps=client.abort_config_flow
        ) as abort,
        caplog.at_level(logging.WARNING),
        pytest.raises(CreationFlowError) as caught,
    ):
        await recreate_flow_helper(client, DOMAIN, _config())

    assert caught.value.apply_status == status
    assert caught.value.reason == reason
    assert "private" not in str(caught.value) + caplog.text
    assert abort.await_count == int(aborted)
    assert submit.await_count <= 2
    entries = hass.config_entries.async_entries(DOMAIN)
    expected_identity = (
        entries[0].entry_id if scenario in {"initial_create", "menu_create"} else ""
    )
    assert caught.value.entry_id == expected_identity
    if scenario == "form_validation":
        assert caught.value.fields == ("additional_options.availability",)


@pytest.mark.parametrize("during_start", [False, True])
async def test_complete_recreation_propagates_cancellation_without_aborting(
    core_client: HomeAssistantClient, run: _Run, during_start: bool
) -> None:
    run.scenario = "cancel_start" if during_start else "cancel_submit"
    client = core_client
    with patch.object(
        client, "abort_config_flow", wraps=client.abort_config_flow
    ) as abort:
        task = asyncio.ensure_future(recreate_flow_helper(client, DOMAIN, _config()))
        # Cancel while the request waits on Core's answer.
        await run.reached.wait()
        task.cancel()
        outcome = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        run.release.set()
        await run.done.wait()
    abort.assert_not_awaited()
