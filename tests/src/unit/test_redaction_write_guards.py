"""A redacted value is never written back through a flow (#2157).

A caller that copies a redacted read into a write would store the marker
over the real credential. Every flow write refuses a value that is, or
holds, a redaction sentinel or the component's ``**redacted**`` before it
opens a flow, whether redaction is on or off: a value read while it was on
must not overwrite a credential after it is turned off. Leaving the key out
keeps the stored value, so nothing is lost.

The flows are Core's, of test integrations and of the template helper, run
through the server's own client; spies on the client show what was opened.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.redaction import REDACTED_EMPTY, REDACTED_KNOWN, REDACTED_SET
from ha_mcp.tools.config_entry_flow import (
    OptionsFlowError,
    restore_config_entry_options,
)
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import open_flow_ids
from ._redaction_forms_core import STORED, add_entry
from ._redaction_forms_core import redaction_core as redaction_core
from ._redaction_forms_core import redaction_flows as redaction_flows
from ._redaction_registry import clean_registry as clean_registry
from ._tool_errors import error_of


@pytest.fixture
async def client(
    redaction_core: None, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return core_client


def _spy(client: HomeAssistantClient, method: str) -> Any:
    return patch.object(client, method, AsyncMock(wraps=getattr(client, method)))


# --- what counts as a redacted value -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "redact"),
    [
        (REDACTED_SET, True),
        (REDACTED_SET, False),
        ("**redacted**", False),
        (f"postgres://u:{REDACTED_KNOWN}@db", True),
    ],
    ids=["sentinel", "sentinel-redaction-off", "component-scrub", "inside-a-value"],
)
async def test_an_options_update_with_a_redacted_value_is_refused_before_a_flow(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    redact: bool,
) -> None:
    monkeypatch.setattr(get_global_settings(), "redact_secrets", redact)
    entry = add_entry(hass)

    with (
        _spy(client, "start_options_flow") as started,
        pytest.raises(ToolError) as exc_info,
    ):
        await IntegrationTools(client).ha_set_integration(
            entry_id=entry.entry_id, config={"advanced": {"inner_token": value}}
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["message"].startswith(
        "config contains redaction placeholder values for: advanced.inner_token."
    )
    started.assert_not_awaited()
    assert open_flow_ids(hass) == []
    assert entry.options == STORED


async def test_an_options_update_with_real_values_is_written(
    hass: HomeAssistant, client: HomeAssistantClient, redact_on: None
) -> None:
    entry = add_entry(hass)

    await IntegrationTools(client).ha_set_integration(
        entry_id=entry.entry_id,
        config={"name": "Hall hub", "advanced": {"inner_token": "tok-NEWSECRET"}},
    )

    assert entry.options["name"] == "Hall hub"
    assert entry.options["advanced"] == {"inner_token": "tok-NEWSECRET"}


# --- every other flow write ----------------------------------------------------------


@pytest.mark.parametrize("placeholder", [True, False], ids=["redacted", "real"])
async def test_an_options_restore_refuses_a_redacted_snapshot_before_reading_core(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    redact_on: None,
    placeholder: bool,
) -> None:
    entry = add_entry(hass)
    api_key = REDACTED_SET if placeholder else "sk-RESTOREDKEY"

    with (
        _spy(client, "get_config_entry") as read,
        _spy(client, "start_options_flow") as started,
    ):
        if placeholder:
            with pytest.raises(OptionsFlowError) as exc_info:
                await restore_config_entry_options(
                    client, entry.entry_id, {**STORED, "api_key": api_key}
                )
            assert exc_info.value.apply_status == "not_applied"
        else:
            await restore_config_entry_options(
                client, entry.entry_id, {**STORED, "api_key": api_key}
            )

    assert (read.await_count, started.await_count) == (
        (0, 0) if placeholder else (1, 1)
    )
    assert entry.options["api_key"] == ("sk-LIVEFLOWSECRET" if placeholder else api_key)


def _fresh_name() -> str:
    """A fresh name: Core restores a removed helper's registry entry by name."""
    return f"Room Temp {uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("placeholder", [True, False], ids=["redacted", "real"])
async def test_a_helper_flow_refuses_a_redacted_value_before_it_opens(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    redact_on: None,
    placeholder: bool,
) -> None:
    """The component reads a template equal to a ``secrets.yaml`` value as
    ``**redacted**``."""
    state = "**redacted**" if placeholder else "{{ 1 }}"

    async def call() -> None:
        await HelperConfigTools(client).ha_config_set_helper(
            helper_type="template",
            menu_choice="sensor",
            config={"name": _fresh_name(), "state": state},
            wait=False,
        )

    with _spy(client, "start_config_flow") as started:
        if placeholder:
            with pytest.raises(ToolError) as exc_info:
                await call()
            assert "state" in error_of(exc_info)["message"]
        else:
            await call()

    assert started.await_count == (0 if placeholder else 1)
    assert len(hass.config_entries.async_entries("template")) == (
        0 if placeholder else 1
    )


_PARENT = "redact_parent"


class _DeviceFlow(ConfigSubentryFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Device", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({probatio.Optional("token"): str}),
        )


class _ParentFlow(ConfigFlow):
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"device": _DeviceFlow}


@pytest.fixture
def parent(hass: HomeAssistant) -> Iterator[MockConfigEntry]:
    mock_integration(hass, MockModule(_PARENT))
    mock_platform(hass, f"{_PARENT}.config_flow", None)
    entry = MockConfigEntry(domain=_PARENT)
    entry.add_to_hass(hass)
    with mock_config_flow(_PARENT, _ParentFlow):
        yield entry


@pytest.mark.parametrize("placeholder", [True, False], ids=["redacted", "real"])
async def test_a_subentry_flow_refuses_a_redacted_value_before_it_opens(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    parent: MockConfigEntry,
    redact_on: None,
    placeholder: bool,
) -> None:
    token = REDACTED_EMPTY if placeholder else "tok-DEVICE01"

    async def call() -> None:
        await HelperConfigTools(client).ha_config_set_helper(
            helper_type="config_subentry",
            entry_id=parent.entry_id,
            subentry_type="device",
            config={"token": token},
        )

    with _spy(client, "start_config_subentry_flow") as started:
        if placeholder:
            with pytest.raises(ToolError) as exc_info:
                await call()
            assert "token" in error_of(exc_info)["message"]
        else:
            await call()

    assert started.await_count == (0 if placeholder else 1)
    stored = [dict(sub.data) for sub in parent.subentries.values()]
    assert stored == ([] if placeholder else [{"token": token}])
