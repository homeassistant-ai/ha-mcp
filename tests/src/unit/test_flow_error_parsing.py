"""Home Assistant's answers to a flow step as structured tool errors.

When Home Assistant rejects a step (a 400, a form back with ``errors``, an
``abort``), the error carries what failed, Core's own text for it where Core
ships one, and the rejected form's fields, so the caller can answer the step
again (issues #1149, #1150). Every flow below is Core's own, driven through
the server's client on a real ``hass``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryDataWithId,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, mock_config_flow

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_entry_flow_errors import FlowRef
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import (
    answer_translations,
    mock_flow_integration,
    open_flow_ids,
)
from ._tool_errors import error_body

_DOMAIN = "flow_error_texts"
_SHARED = "flow_error_shared"
_ABORTING = "flow_error_aborting"
_UNIQUE_ID = "flow-error-device"

_STRINGS = {
    "config": {"abort": {"another_device": "Wrong device."}},
    "options": {"error": {"cannot_connect": "Cannot reach {host}."}},
    "config_subentries": {
        "conversation": {"abort": {"entry_not_loaded": "The entry is not loaded."}}
    },
}


def _host_form(
    flow: ConfigFlow | OptionsFlow | ConfigSubentryFlow,
    step_id: str,
    stored: dict[str, Any],
    errors: dict[str, str] | None = None,
    placeholders: dict[str, str] | None = None,
) -> Any:
    schema = probatio.Schema(
        {
            probatio.Required("host"): selector.TextSelector(),
            probatio.Optional("api_key"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )
    return flow.async_show_form(
        step_id=step_id,
        data_schema=flow.add_suggested_values_to_schema(schema, stored),
        errors=errors,
        description_placeholders=placeholders,
    )


class _OptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return _host_form(self, "init", dict(self.config_entry.options))
        return _host_form(
            self,
            "init",
            dict(self.config_entry.options),
            errors={"base": "cannot_connect", "host": "no_string_for_this"},
            placeholders={"host": user_input["host"]},
        )


class _DeviceFlow(ConfigSubentryFlow):
    """Creates and reconfigures a ``device``; every answer is refused."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors = None if user_input is None else {"base": "cannot_connect"}
        return _host_form(self, "user", {}, errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors = None if user_input is None else {"base": "cannot_connect"}
        return _host_form(self, "reconfigure", {"host": "h"}, errors)


class _ConversationFlow(ConfigSubentryFlow):
    """Aborts as its entry's ``abort`` data says."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        abort = self._get_entry().data["abort"]
        return self.async_abort(
            reason=abort["reason"], translation_domain=abort.get("translation_domain")
        )


class _ConfigFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        sensor = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({probatio.Required("entity_id"): sensor}),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        stored = dict(self._get_reconfigure_entry().data)
        if user_input is None:
            return _host_form(self, "reconfigure", stored)
        if user_input["host"] == "10.0.0.3":
            return self.async_abort(reason="another_device")
        return _host_form(self, "reconfigure", stored, {"base": "not_valid"})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _OptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"device": _DeviceFlow, "conversation": _ConversationFlow}


class _AbortingFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="no_devices_found")


@pytest.fixture
def flows(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The test integrations, with the strings ``_DOMAIN`` and ``_SHARED`` ship."""
    mock_flow_integration(hass, _DOMAIN, tmp_path, monkeypatch, _STRINGS)
    mock_flow_integration(
        hass,
        _SHARED,
        tmp_path,
        monkeypatch,
        {"config": {"abort": {"cloud_not_connected": "Log in first."}}},
    )
    mock_flow_integration(hass, _ABORTING, tmp_path, monkeypatch)
    with (
        mock_config_flow(_DOMAIN, _ConfigFlow),
        mock_config_flow(_ABORTING, _AbortingFlow),
    ):
        yield


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, flows: None
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a ``hass`` that serves flows and their strings."""
    assert await async_setup_component(hass, "config", {})
    answer_translations(hass)
    yield component_core_client
    await hass.async_block_till_done()


def _entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=_DOMAIN,
        unique_id=_UNIQUE_ID,
        data={"host": "10.0.0.1", **data},
        options={"host": "10.0.0.1", "api_key": "sk-LIVE"},
        subentries_data=[
            ConfigSubentryDataWithId(
                data={"host": "h"},
                subentry_id="s1",
                subentry_type="device",
                title="Device",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


def _translation_requests(sent: AsyncMock) -> list[tuple[str, list[str]]]:
    return [
        (c.args[0]["category"], c.args[0]["integration"])
        for c in sent.await_args_list
        if c.args[0].get("type") == "frontend/get_translations"
    ]


async def _reconfigure(
    client: HomeAssistantClient, entry: MockConfigEntry, config: dict[str, Any]
) -> Any:
    return await IntegrationTools(client).ha_set_integration(
        entry_id=entry.entry_id,
        reconfigure=True,
        expected_unique_id=_UNIQUE_ID,
        config=config,
    )


async def _set_subentry(
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    subentry_type: str,
    config: dict[str, Any],
    subentry_id: str | None = None,
) -> Any:
    return await HelperConfigTools(client).ha_config_set_helper(
        helper_type="config_subentry",
        entry_id=entry.entry_id,
        subentry_type=subentry_type,
        subentry_id=subentry_id,
        config=config,
    )


class TestRejectedSubmit:
    """A 400 on a submit says what failed and shows the rejected form."""

    async def test_field_errors_come_with_the_rejected_forms_schema(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        with (
            patch.object(
                client, "abort_config_flow", wraps=client.abort_config_flow
            ) as aborted,
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                domain=_DOMAIN, config={"entity_id": "light.foo"}
            )

        body = error_body(exc_info)
        assert body["success"] is False
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert list(body["field_errors"]) == ["entity_id"]
        assert body["status_code"] == 400
        assert [f["name"] for f in body["data_schema"]] == ["entity_id"]
        # The flow stays open to be answered again.
        aborted.assert_not_called()
        assert open_flow_ids(hass) == [body["flow_id"]]

    async def test_a_rejected_reconfigure_submit_reports_nothing_applied(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = _entry(hass)

        with pytest.raises(ToolError) as exc_info:
            await _reconfigure(client, entry, {"host": ["not", "text"]})

        body = error_body(exc_info)
        assert list(body["field_errors"]) == ["host"]
        assert [f["name"] for f in body["data_schema"]] == ["host", "api_key"]
        assert body["status"] == "apply_failed"
        assert entry.data["host"] == "10.0.0.1"


class TestFlowTextFromCore:
    """The tool error carries Core's string for an abort reason or form error."""

    async def test_abort_without_a_core_string_keeps_only_the_reason(
        self, client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await IntegrationTools(client).ha_set_integration(domain=_ABORTING)

        body = error_body(exc_info)
        assert "no_devices_found" in body["error"]["message"]
        assert "reason_text" not in body
        assert "warnings" not in body

    async def test_options_form_errors_carry_cores_text_with_placeholders(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = _entry(hass)

        with (
            patch.object(
                client, "send_websocket_message", wraps=client.send_websocket_message
            ) as sent,
            patch.object(
                client,
                "submit_options_flow_step",
                wraps=client.submit_options_flow_step,
            ) as submitted,
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id=entry.entry_id, config={"host": "10.0.0.2"}
            )

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        # The raw keys stay; a key Core ships no string for has no text.
        assert body["errors"] == {
            "base": "cannot_connect",
            "host": "no_string_for_this",
        }
        assert body["error_text"] == {"base": "Cannot reach 10.0.0.2."}
        assert [f["name"] for f in body["fields"]] == ["host", "api_key"]
        assert "data_schema" not in body
        assert _translation_requests(sent) == [("options", [_DOMAIN])]
        submitted.assert_awaited_once()
        assert open_flow_ids(hass) == [body["flow_id"]]

    async def test_subentry_abort_reads_its_type_under_the_parent_domain(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = _entry(hass, abort={"reason": "entry_not_loaded"})

        with (
            patch.object(
                client, "send_websocket_message", wraps=client.send_websocket_message
            ) as sent,
            pytest.raises(ToolError) as exc_info,
        ):
            await _set_subentry(client, entry, "conversation", {})

        assert error_body(exc_info)["reason_text"] == "The entry is not loaded."
        assert _translation_requests(sent) == [("config_subentries", [_DOMAIN])]

    async def test_abort_translation_domain_names_where_the_reason_lives(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        # translation_domain names the integration that owns the reason; a
        # subentry flow then reads it under that domain's config strings, as
        # the frontend's dev branch does (the release Core 2026.8.0 pins reads
        # config_subentries.<type>.abort).
        entry = _entry(
            hass,
            abort={"reason": "cloud_not_connected", "translation_domain": _SHARED},
        )

        with (
            patch.object(
                client, "send_websocket_message", wraps=client.send_websocket_message
            ) as sent,
            pytest.raises(ToolError) as exc_info,
        ):
            await _set_subentry(client, entry, "conversation", {})

        assert error_body(exc_info)["reason_text"] == "Log in first."
        assert _translation_requests(sent) == [("config", [_SHARED])]

    async def test_reconfigure_abort_carries_cores_reason_text(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = _entry(hass)

        with pytest.raises(ToolError) as exc_info:
            await _reconfigure(client, entry, {"host": "10.0.0.3"})

        body = error_body(exc_info)
        assert body["reason_text"] == "Wrong device."
        assert body["status"] == "apply_failed"

    async def test_a_reconfigure_form_back_with_errors_is_refused_and_stays_open(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = _entry(hass)

        with (
            patch.object(
                client,
                "submit_config_flow_step",
                wraps=client.submit_config_flow_step,
            ) as submitted,
            patch.object(
                client, "abort_config_flow", wraps=client.abort_config_flow
            ) as aborted,
            pytest.raises(ToolError) as exc_info,
        ):
            await _reconfigure(client, entry, {"host": "10.0.0.9"})

        body = error_body(exc_info)
        assert open_flow_ids(hass) == [body["flow_id"]]
        assert body["errors"] == {"base": "not_valid"}
        assert body["status"] == "apply_failed"
        submitted.assert_awaited_once()
        aborted.assert_not_awaited()

    @pytest.mark.parametrize(
        ("subentry_id", "status"), [("s1", "apply_failed"), (None, None)]
    )
    async def test_a_subentry_form_back_with_errors_is_refused(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        subentry_id: str | None,
        status: str | None,
    ) -> None:
        entry = _entry(hass)

        with (
            patch.object(
                client,
                "submit_config_subentry_flow_step",
                wraps=client.submit_config_subentry_flow_step,
            ) as submitted,
            patch.object(
                client,
                "abort_config_subentry_flow",
                wraps=client.abort_config_subentry_flow,
            ) as aborted,
            pytest.raises(ToolError) as exc_info,
        ):
            await _set_subentry(client, entry, "device", {"host": "x"}, subentry_id)

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["errors"] == {"base": "cannot_connect"}
        assert body.get("status") == status
        submitted.assert_awaited_once()
        aborted.assert_not_awaited()
        assert open_flow_ids(hass) == [body["flow_id"]]


@pytest.mark.usefixtures("redact_on")
async def test_form_errors_redact_the_schemas_password_value(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    # The options form carries the stored api_key in suggested_value.
    entry = _entry(hass)

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id=entry.entry_id, config={"host": "10.0.0.2"}
        )

    assert "sk-LIVE" not in str(exc_info.value)
    assert "api_key" in [f["name"] for f in error_body(exc_info)["fields"]]


@pytest.mark.parametrize("path", ["config", "options", "config_subentries.device"])
def test_a_flow_ref_takes_the_flow_categories_core_has(path: str) -> None:
    assert FlowRef("demo", path).path == path


@pytest.mark.parametrize("path", ["", "option", "subentries.device"])
def test_a_flow_ref_refuses_a_path_core_has_no_flow_for(path: str) -> None:
    with pytest.raises(ValueError, match="flow path"):
        FlowRef("demo", path)


async def test_a_form_error_whose_strings_cannot_be_read_warns_at_top_level(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, flows: None
) -> None:
    # Nothing registered frontend/get_translations: Core refuses the read.
    assert await async_setup_component(hass, "config", {})
    entry = _entry(hass)

    with pytest.raises(ToolError) as exc_info:
        await _reconfigure(component_core_client, entry, {"host": "10.0.0.9"})

    body = error_body(exc_info)
    assert body["errors"] == {"base": "not_valid"}
    assert "error_text" not in body
    assert any("could not be read" in w for w in body["warnings"])
