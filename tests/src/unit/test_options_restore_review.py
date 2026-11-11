"""Options restore diagnostics preserve safe paths and identify unaborted flows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_entry_flow import OptionsFlowError, update_config_entry_options

DOMAIN = "restore_review"

# The form every scenario's flow shows: two sections, neither declaring a field.
FORM = probatio.Schema(
    {
        probatio.Required("state"): str,
        probatio.Optional("additional_options"): section(
            probatio.Schema({}), {"collapsed": True}
        ),
        probatio.Optional("secret_marker_section"): section(probatio.Schema({})),
    }
)

# Flow ids Home Assistant gave the review flows, in the order they opened.
opened: list[str] = []
# The "cancelled" flow holds the submit until the test releases it.
held: dict[str, asyncio.Event] = {}


class _ReviewOptionsFlow(OptionsFlow):
    """Another integration's options flow, answering as its entry's scenario says."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.config_entry.data["scenario"]
        if user_input is None:
            opened.append(self.flow_id)
            if scenario == "applied_initial":
                return self.async_create_entry(
                    data={"secret_marker_key": "secret-marker-value"}
                )
            errors = None
            if scenario == "validation":
                errors = {
                    "additional_options.availability": "secret-marker-error",
                    "additional_options.secret_marker_leaf": "secret-marker-error",
                    "secret_marker_section.availability": "secret-marker-error",
                }
            return self.async_show_form(
                step_id="init", data_schema=FORM, errors=errors, last_step=True
            )
        if scenario == "unknown_step":
            return self.async_external_step(
                step_id="init", url="https://example.com/secret-marker"
            )
        if scenario == "cancelled":
            held["entered"].set()
            try:
                await held["release"].wait()
            finally:
                held["left"].set()
        return self.async_create_entry(data=user_input)


class _ReviewConfigFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _ReviewOptionsFlow()


@pytest.fixture
async def review_integration(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[None]:
    opened.clear()
    held.update(entered=asyncio.Event(), release=asyncio.Event(), left=asyncio.Event())
    mock_integration(hass, MockModule(DOMAIN))
    mock_platform(hass, f"{DOMAIN}.config_flow", None)
    with mock_config_flow(DOMAIN, _ReviewConfigFlow):
        assert await async_setup_component(hass, "config", {})
        yield


def _entry(hass: HomeAssistant, scenario: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"scenario": scenario})
    entry.add_to_hass(hass)
    return entry


async def _restore(
    client: HomeAssistantClient, entry_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    return await update_config_entry_options(
        client, entry_id, config, expected_domain=DOMAIN, keep_current_values=False
    )


async def _cancel_during_submit(client: HomeAssistantClient, entry_id: str) -> None:
    """Cancel the restore while Home Assistant is still handling its submit."""
    restore = asyncio.create_task(
        _restore(client, entry_id, {"state": "secret-marker-template"})
    )
    try:
        async with asyncio.timeout(10):
            await held["entered"].wait()
        restore.cancel("secret-marker-cancellation")
        await restore
    finally:
        restore.cancel()
        held["release"].set()


@pytest.mark.parametrize("scenario", ["unsupported", "validation"])
async def test_nested_restore_fields_preserve_only_fully_safe_paths(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    review_integration: None,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
) -> None:
    entry = _entry(hass, scenario)
    config: dict[str, Any] = {"state": "secret-marker-template"}
    if scenario == "unsupported":
        config.update(
            additional_options={
                "availability": "secret-marker-template",
                "secret_marker_leaf": "secret-marker-value",
            },
            secret_marker_section={"availability": "secret-marker-template"},
        )

    with (
        patch.object(
            core_client,
            "submit_options_flow_step",
            wraps=core_client.submit_options_flow_step,
        ) as submit,
        patch.object(
            core_client, "abort_options_flow", wraps=core_client.abort_options_flow
        ) as abort,
        caplog.at_level(logging.WARNING),
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(core_client, entry.entry_id, config)

    assert caught.value.fields == ("additional_options.availability",)
    assert "additional_options.availability" in str(caught.value)
    assert "secret_marker" not in str(caught.value) + caplog.text
    assert "secret-marker" not in str(caught.value) + caplog.text
    submit.assert_not_awaited()
    assert len(opened) == 1
    abort.assert_awaited_once_with(opened[0])
    assert hass.config_entries.options.async_progress() == []


async def test_restore_abort_cleanup_logs_safe_failure_context(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    review_integration: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _entry(hass, "unsupported")

    with (
        patch.object(
            core_client,
            "submit_options_flow_step",
            wraps=core_client.submit_options_flow_step,
        ) as submit,
        patch.object(
            core_client,
            "abort_options_flow",
            AsyncMock(side_effect=HomeAssistantConnectionError("PRIVATE_TEST_PAYLOAD")),
        ) as abort,
        caplog.at_level(logging.WARNING),
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(
            core_client,
            entry.entry_id,
            {"state": "secret-marker-template", "unit_of_measurement": "secret-marker"},
        )

    assert caught.value.reason == "unsupported_fields"
    assert caught.value.apply_status == "not_applied"
    # Not turned into a structured error, which would log its text.
    assert caught.value.__cause__ is None
    assert "Unclassified exception" not in caplog.text
    submit.assert_not_awaited()
    assert len(opened) == 1
    abort.assert_awaited_once_with(opened[0])
    assert "PRIVATE_TEST_PAYLOAD" not in caplog.text
    assert "secret-marker" not in caplog.text
    assert opened[0] in caplog.text
    assert entry.entry_id in caplog.text
    assert "stage=abort_cleanup" in caplog.text
    assert "reason=unsupported_fields" in caplog.text
    assert "error_type=HomeAssistantConnectionError" in caplog.text


async def _run_unaborted(
    client: HomeAssistantClient, entry_id: str, scenario: str
) -> None:
    if scenario == "cancelled":
        await _cancel_during_submit(client, entry_id)
    elif scenario == "connection":
        with patch.object(
            client,
            "submit_options_flow_step",
            AsyncMock(
                side_effect=HomeAssistantConnectionError("secret-marker-transport")
            ),
        ):
            await _restore(client, entry_id, {"state": "secret-marker-template"})
    else:
        await _restore(client, entry_id, {"state": "secret-marker-template"})


@pytest.mark.parametrize(
    ("scenario", "apply_status", "error_type"),
    [
        ("connection", "unknown", OptionsFlowError),
        ("unknown_step", "unknown", OptionsFlowError),
        ("applied_initial", "applied", OptionsFlowError),
        ("cancelled", "unknown", asyncio.CancelledError),
    ],
)
async def test_unaborted_restore_logs_reconciliation_context_without_values(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    review_integration: None,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    apply_status: str,
    error_type: type[BaseException],
) -> None:
    entry = _entry(hass, scenario)

    with (
        patch.object(
            core_client, "abort_options_flow", wraps=core_client.abort_options_flow
        ) as abort,
        caplog.at_level(logging.WARNING),
        pytest.raises(error_type),
    ):
        await _run_unaborted(core_client, entry.entry_id, scenario)

    abort.assert_not_awaited()
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert len(opened) == 1
    assert opened[0] in message
    assert entry.entry_id in message
    assert f"apply_status={apply_status}" in message
    assert "not aborted" in message
    assert "reconcile" in message.lower()
    assert "secret_marker" not in caplog.text
    assert "secret-marker" not in caplog.text
    if scenario == "cancelled":
        # Let Home Assistant finish the submit it was still handling.
        async with asyncio.timeout(10):
            await held["left"].wait()
