"""Config subentries through ha_get_integration, ha_config_set_helper and
ha_remove_helpers_integrations, on a real ``hass``.

Every subentry flow below is Core's, run through the server's own client
(``core_client``), so each form, refusal and stored subentry is Core's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryDataWithId,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
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
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import open_flow_ids
from ._tool_errors import rejected_call_body

_DOMAIN = "subentries_folded"
_EXISTING = "existing"


class _ModelFlow(ConfigSubentryFlow):
    """One form: a name, and a token limit Core checks is a number."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.pop("name"), data=user_input
            )
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {probatio.Required("name"): str, probatio.Required("max_tokens"): int}
            ),
        )


class _StagedFlow(ConfigSubentryFlow):
    """A name on the first form, the model on a second."""

    name = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            self.name = user_input["name"]
            return await self.async_step_model()
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({probatio.Required("name"): str}),
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=self.name, data=user_input)
        return self.async_show_form(
            step_id="model",
            data_schema=probatio.Schema({probatio.Required("model"): str}),
        )


class _MenuFlow(ConfigSubentryFlow):
    """Opens on a menu whose one option leads to a form."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["conversation"])

    async def async_step_conversation(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Local Model", data=user_input)
        return self.async_show_form(
            step_id="conversation",
            data_schema=probatio.Schema({probatio.Required("model"): str}),
        )


class _ParentFlow(ConfigFlow):
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"model": _ModelFlow, "staged": _StagedFlow, "menu": _MenuFlow}


@pytest.fixture
def parent(hass: HomeAssistant) -> Iterator[MockConfigEntry]:
    """An entry that already holds one ``model`` subentry."""
    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(hass, f"{_DOMAIN}.config_flow", None)
    entry = MockConfigEntry(
        domain=_DOMAIN,
        subentries_data=[
            ConfigSubentryDataWithId(
                data={"max_tokens": 1},
                subentry_id=_EXISTING,
                subentry_type="model",
                title="Existing",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    with mock_config_flow(_DOMAIN, _ParentFlow):
        yield entry


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    """``core_client`` on a ``hass`` whose ``config`` serves the flow endpoints."""
    assert await async_setup_component(hass, "config", {})
    yield core_client
    await hass.async_block_till_done()


async def _get(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(client).ha_get_integration(**kwargs)
    return result


async def _set_subentry(
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    subentry_type: str,
    **kwargs: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = await HelperConfigTools(client).ha_config_set_helper(
        helper_type="config_subentry",
        entry_id=entry.entry_id,
        subentry_type=subentry_type,
        **kwargs,
    )
    return result


async def _remove_subentry(
    client: HomeAssistantClient, entry: MockConfigEntry, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target=entry.entry_id, helper_type="config_subentry", **kwargs
    )
    return result


# --- ha_get_integration ------------------------------------------------------------


async def test_an_entrys_subentries_are_listed_without_the_component(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    result = await _get(client, entry_id=parent.entry_id, include_subentries=True)

    assert result["subentry_count"] == 1
    (subentry,) = result["subentries"]
    assert (subentry["subentry_id"], subentry["title"]) == (_EXISTING, "Existing")


async def test_a_subentry_schema_read_shows_the_form_and_closes_its_flow(
    hass: HomeAssistant, parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    """The flow is opened only to read its form: none is left open in Home
    Assistant, and show_advanced_options reaches it (Home Assistant before
    2026.6 reads it)."""
    with patch.object(
        client, "start_config_subentry_flow", wraps=client.start_config_subentry_flow
    ) as started:
        result = await _get(
            client,
            entry_id=parent.entry_id,
            include_subentry_schema=True,
            subentry_type="model",
            show_advanced_options=True,
        )

    schema = result["subentry_schema"]
    assert schema["flow_type"] == "form"
    assert [field["name"] for field in schema["fields"]] == ["name", "max_tokens"]
    assert open_flow_ids(hass) == []
    started.assert_awaited_once_with(
        parent.entry_id, "model", subentry_id=None, show_advanced_options=True
    )


async def test_a_subentry_menu_schema_shows_the_menu_and_closes_its_flow(
    hass: HomeAssistant, parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    result = await _get(
        client,
        entry_id=parent.entry_id,
        include_subentry_schema=True,
        subentry_type="menu",
    )

    schema = result["subentry_schema"]
    assert schema["flow_type"] == "menu"
    assert schema["menu_options"] == ["conversation"]
    assert open_flow_ids(hass) == []


async def test_a_failed_close_of_the_read_flow_still_returns_the_form(
    hass: HomeAssistant,
    parent: MockConfigEntry,
    client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The form was read: losing the abort costs a warning in the log, not
    the answer. The flow it could not close is still open in Core."""
    monkeypatch.setattr(
        client,
        "abort_config_subentry_flow",
        AsyncMock(side_effect=HomeAssistantConnectionError("slow abort")),
    )

    with caplog.at_level(logging.WARNING):
        result = await _get(
            client,
            entry_id=parent.entry_id,
            include_subentry_schema=True,
            subentry_type="model",
        )

    assert [field["name"] for field in result["subentry_schema"]["fields"]] == [
        "name",
        "max_tokens",
    ]
    assert "abort failed: slow abort" in caplog.text
    assert len(open_flow_ids(hass)) == 1


async def test_a_subentry_schema_read_without_a_type_is_refused_unopened(
    hass: HomeAssistant, parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(
        _get(client, entry_id=parent.entry_id, include_subentry_schema=True)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_type" in body["error"]["message"]
    assert open_flow_ids(hass) == []


async def test_a_user_core_will_not_list_subentries_for_gets_cores_refusal(
    parent: MockConfigEntry,
    client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
) -> None:
    """Listing subentries is admin-only in Core; a non-admin's read fails
    with Core's reason instead of an empty list."""
    body = await rejected_call_body(
        _get(read_only_client, entry_id=parent.entry_id, include_subentries=True)
    )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    message = body["error"]["message"]
    assert "Failed to list config subentries" in message
    assert "Unauthorized" in message


async def test_list_mode_warns_only_about_the_detail_parameters_given(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    result = await _get(client, include_subentries=True, show_advanced_options=True)

    (warning,) = [w for w in result["warnings"] if "include_subentries" in w]
    assert "show_advanced_options" in warning
    assert "include_diagnostics" not in warning


# --- ha_config_set_helper ----------------------------------------------------------


async def test_a_created_subentry_is_named_by_its_id_beside_an_existing_one(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    """Core's create result carries no id: the new one is found among the
    entry's subentries, not mistaken for the one already there.
    show_advanced_options reaches the flow (Home Assistant before 2026.6)."""
    with patch.object(
        client, "start_config_subentry_flow", wraps=client.start_config_subentry_flow
    ) as started:
        created = await _set_subentry(
            client,
            parent,
            "model",
            config={"name": "Local Model", "max_tokens": 8},
            show_advanced_options=True,
        )

    assert created["operation"] == "created"
    assert created["subentry_id"] != _EXISTING
    assert parent.subentries[created["subentry_id"]].title == "Local Model"
    started.assert_awaited_once_with(
        parent.entry_id, "model", subentry_id=None, show_advanced_options=True
    )


async def test_concurrent_creates_each_return_their_own_subentry(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    """Two creates under one entry at once: each is told the id of the
    subentry it made, not None or the other's."""
    first, second = await asyncio.gather(
        *(
            _set_subentry(client, parent, "model", config={"name": n, "max_tokens": 1})
            for n in ("m", "n")
        )
    )

    assert parent.subentries[first["subentry_id"]].title == "m"
    assert parent.subentries[second["subentry_id"]].title == "n"


async def test_a_later_subentry_form_is_answered_in_its_own_call(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    """Nothing is saved until the second form is answered with the flow_id."""
    shown = await _set_subentry(
        client, parent, "staged", config={"name": "Local Model"}
    )

    assert shown["flow_type"] == "form"
    assert shown["step_id"] == "model"
    assert list(parent.subentries) == [_EXISTING]

    created = await _set_subentry(
        client,
        parent,
        "staged",
        flow_id=shown["flow_id"],
        config={"model": "gemma3:27b"},
    )

    subentry = parent.subentries[created["subentry_id"]]
    assert (subentry.title, dict(subentry.data)) == (
        "Local Model",
        {"model": "gemma3:27b"},
    )


async def test_a_subentry_menu_is_answered_with_menu_choice(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    created = await _set_subentry(
        client,
        parent,
        "menu",
        menu_choice="conversation",
        config={"model": "gemma3:27b"},
    )

    assert created["operation"] == "created"
    assert dict(parent.subentries[created["subentry_id"]].data) == {
        "model": "gemma3:27b"
    }


async def test_a_rejected_subentry_submit_shows_the_form_and_keeps_the_flow(
    hass: HomeAssistant, parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    """Core refuses a non-number token limit: the error names the field and
    shows the form, and the same flow takes the corrected answer."""
    body = await rejected_call_body(
        _set_subentry(
            client,
            parent,
            "model",
            config={"name": "Local Model", "max_tokens": "lots"},
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert list(body["field_errors"]) == ["max_tokens"]
    assert [field["name"] for field in body["data_schema"]] == ["name", "max_tokens"]
    assert body["submitted_keys"] == ["max_tokens", "name"]
    assert open_flow_ids(hass) == [body["flow_id"]]

    created = await _set_subentry(
        client,
        parent,
        "model",
        flow_id=body["flow_id"],
        config={"name": "Local Model", "max_tokens": 8},
    )
    assert parent.subentries[created["subentry_id"]].title == "Local Model"


async def test_an_update_without_a_subentry_id_is_refused_unopened(
    hass: HomeAssistant, parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(
        _set_subentry(client, parent, "model", action="update", config={"x": 1})
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_id" in body["error"]["message"]
    assert open_flow_ids(hass) == []


async def test_config_that_is_not_json_is_refused_unopened(
    hass: HomeAssistant, parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(
        _set_subentry(client, parent, "model", config="{not-json")
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["parameter"] == "config"
    assert open_flow_ids(hass) == []


async def test_an_open_subentry_flow_is_ended_with_abort(
    hass: HomeAssistant,
    parent: MockConfigEntry,
    client: HomeAssistantClient,
    opened_flows: dict[str, tuple[str, str | None]],
) -> None:
    shown = await _set_subentry(client, parent, "model")
    assert open_flow_ids(hass) == [shown["flow_id"]]

    aborted = await _set_subentry(
        client, parent, "model", flow_id=shown["flow_id"], abort=True
    )

    assert aborted["aborted"] is True
    assert open_flow_ids(hass) == []
    assert shown["flow_id"] not in opened_flows


# --- ha_remove_helpers_integrations ------------------------------------------------


async def test_a_subentry_is_removed_from_its_entry(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    result = await _remove_subentry(client, parent, subentry_id=_EXISTING, confirm=True)

    assert result["method"] == "config_subentry_delete"
    assert result["subentry_id"] == _EXISTING
    assert _EXISTING not in parent.subentries


async def test_a_subentry_removal_without_confirm_removes_nothing(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(
        _remove_subentry(client, parent, subentry_id=_EXISTING, confirm=False)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Deletion not confirmed" in body["error"]["message"]
    assert _EXISTING in parent.subentries


async def test_a_subentry_removal_without_a_subentry_id_removes_nothing(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(_remove_subentry(client, parent, confirm=True))

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_id" in body["error"]["message"]
    assert _EXISTING in parent.subentries


async def test_removing_a_missing_subentry_says_it_was_not_found(
    parent: MockConfigEntry, client: HomeAssistantClient
) -> None:
    """Core answers not_found: a typo'd subentry_id is reported as missing,
    with how to check, instead of as a success or a generic failure."""
    body = await rejected_call_body(
        _remove_subentry(client, parent, subentry_id="ghost-subentry", confirm=True)
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    message = body["error"]["message"]
    assert "ghost-subentry" in message
    assert "ha_get_integration" in message


async def test_a_refused_subentry_removal_is_a_failure_not_a_missing_subentry(
    parent: MockConfigEntry,
    client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
) -> None:
    """Core refuses a non-admin: that is reported as Core's failure, not as
    the subentry being already gone."""
    body = await rejected_call_body(
        _remove_subentry(read_only_client, parent, subentry_id=_EXISTING, confirm=True)
    )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unauthorized" in body["error"]["message"]
    assert _EXISTING in parent.subentries
