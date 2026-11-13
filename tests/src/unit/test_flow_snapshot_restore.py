"""Complete options restores preserve payload and submission knowledge.

Another integration's options flow runs in a real ``hass`` and answers as its
entry's scenario says; the server's client drives it over Core's REST API.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from unittest.mock import ANY, AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_entry_flow import (
    OptionsFlowError,
    restore_config_entry_options,
)
from ha_mcp.tools.config_entry_flow_form import form_submission

from ._mock_flow_integration import options_only_config_flow

_DOMAIN = "flow_snapshot_probe"
_SHOWN = {"additional_options": {"availability": "{{ false }}"}}


class _Hold:
    """The ``hold`` scenario keeps the submit inside Core until released."""

    entered: ClassVar[asyncio.Event]
    release: ClassVar[asyncio.Event]
    left: ClassVar[asyncio.Event]


def _button_form(flow: OptionsFlow, **kwargs: Any) -> ConfigFlowResult:
    schema = probatio.Schema(
        {
            probatio.Optional("press"): selector.ActionSelector(),
            probatio.Optional("additional_options"): section(
                probatio.Schema(
                    {probatio.Optional("availability"): selector.TemplateSelector()}
                ),
                {"collapsed": True},
            ),
        }
    )
    return flow.async_show_form(
        step_id="button",
        data_schema=flow.add_suggested_values_to_schema(
            schema, flow.config_entry.options
        ),
        last_step=True,
        **kwargs,
    )


class _ProbeOptionsFlow(OptionsFlow):
    """Another integration's options flow, answering as its entry's scenario says."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.config_entry.data["scenario"]
        if scenario == "menu":
            return self.async_show_menu(step_id="init", menu_options=["button"])
        if scenario == "no_schema":
            return self.async_show_form(step_id="init", last_step=True)
        if scenario == "two_forms":
            if user_input is not None:
                return await self.async_step_presets()
            return self.async_show_form(
                step_id="init",
                data_schema=probatio.Schema(
                    {probatio.Optional("cold_tolerance"): _number()}
                ),
                last_step=False,
            )
        if scenario == "admin_revoked":
            # The caller stops being an administrator while the flow is open.
            for user in await self.hass.auth.async_get_users():
                if not user.system_generated:
                    await self.hass.auth.async_update_user(
                        user, group_ids=["system-users"]
                    )
        return _button_form(self)

    async def async_step_presets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options})
        return self.async_show_form(
            step_id="presets",
            data_schema=probatio.Schema({probatio.Optional("away_temp"): _number()}),
            last_step=True,
        )

    async def async_step_extra(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="extra", last_step=True)

    async def async_step_button(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.config_entry.data["scenario"]
        if scenario == "followup_form":
            return await self.async_step_extra()
        if scenario == "followup_menu":
            return self.async_show_menu(step_id="button", menu_options=["next"])
        if scenario == "rejected":
            return _button_form(self, errors={"press": "invalid"})
        if scenario == "hold":
            _Hold.entered.set()
            try:
                await _Hold.release.wait()
            finally:
                _Hold.left.set()
        return self.async_create_entry(data=user_input or {})


def _number() -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=100))


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    _Hold.entered, _Hold.release, _Hold.left = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(hass, f"{_DOMAIN}.config_flow")
    with mock_config_flow(_DOMAIN, options_only_config_flow(_ProbeOptionsFlow)):
        assert await async_setup_component(hass, "config", {})
        yield core_client
        _Hold.release.set()
        await hass.async_block_till_done()


def _entry(hass: HomeAssistant, scenario: str = "complete") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=_DOMAIN, data={"scenario": scenario}, options=dict(_SHOWN)
    )
    entry.add_to_hass(hass)
    return entry


def _spy(client: HomeAssistantClient, name: str) -> Any:
    return patch.object(client, name, wraps=getattr(client, name))


async def _restore(
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    config: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    return await restore_config_entry_options(
        client, entry.entry_id, config, expected_domain=_DOMAIN, **kwargs
    )


def _open_options_flows(hass: HomeAssistant) -> list[str]:
    return [flow["flow_id"] for flow in hass.config_entries.options.async_progress()]


async def test_explicit_empty_section_completes_without_false_key_error(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass)

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        _spy(client, "abort_options_flow") as aborted,
    ):
        result = await _restore(client, entry, {"additional_options": {}})

    assert result["success"] is True
    submitted.assert_awaited_once_with(ANY, {"additional_options": {}})
    aborted.assert_not_awaited()
    assert entry.options == {"additional_options": {}}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, _SHOWN),
        ({"additional_options": {}}, _SHOWN),
        # An emptied section is posted empty, as the HA dialog posts it.
        ({"additional_options": {"availability": None}}, {"additional_options": {}}),
    ],
)
async def test_ordinary_edit_section_backfill_and_clear(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    values: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    entry = _entry(hass)
    step = await client.start_options_flow(entry.entry_id)
    await client.abort_options_flow(step["flow_id"])

    assert form_submission(step["data_schema"], values, keep_shown=True) == expected


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": 1},
        {"additional_options": {"typo": 1}},
        {"step_values": {"button": {"press": []}}},
        {"next_step_id": "button"},
    ],
)
async def test_unsupported_snapshot_fields_are_refused_before_submit(
    hass: HomeAssistant, client: HomeAssistantClient, config: dict[str, Any]
) -> None:
    entry = _entry(hass)

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        _spy(client, "abort_options_flow") as aborted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(client, entry, config)

    assert caught.value.apply_status == "not_applied"
    submitted.assert_not_awaited()
    aborted.assert_awaited_once_with(caught.value.flow_id)
    assert _open_options_flows(hass) == []


@pytest.mark.parametrize("scenario", ["menu", "no_schema"])
async def test_restore_requires_authoritative_single_form_schema(
    hass: HomeAssistant, client: HomeAssistantClient, scenario: str
) -> None:
    entry = _entry(hass, scenario)

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(client, entry, {"press": []})

    assert caught.value.apply_status == "not_applied"
    submitted.assert_not_awaited()
    assert _open_options_flows(hass) == []


async def test_lost_submit_reply_preserves_unknown_and_does_not_abort(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass)

    with (
        patch.object(
            client,
            "submit_options_flow_step",
            AsyncMock(side_effect=HomeAssistantConnectionError("lost reply")),
        ),
        _spy(client, "abort_options_flow") as aborted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(client, entry, {"press": []})

    assert caught.value.apply_status == "unknown"
    assert caught.value.entry_id == entry.entry_id
    assert _open_options_flows(hass) == [caught.value.flow_id]
    assert "may have been applied" in str(caught.value)
    aborted.assert_not_awaited()


@pytest.mark.parametrize(
    ("scenario", "config", "error_type"),
    [
        # The caller loses admin rights while the flow is open: Core answers 401.
        ("admin_revoked", {"press": []}, HomeAssistantAuthError),
        # Core's schema refuses the submit: 400.
        (
            "complete",
            {"additional_options": {"availability": "{{ unclosed"}},
            HomeAssistantAPIError,
        ),
    ],
    ids=["401", "400"],
)
async def test_http_submit_refusal_is_not_applied_and_aborted(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    scenario: str,
    config: dict[str, Any],
    error_type: type[Exception],
) -> None:
    entry = _entry(hass, scenario)

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        _spy(client, "abort_options_flow") as aborted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(client, entry, config)

    assert isinstance(caught.value.__cause__, error_type)
    assert caught.value.apply_status == "not_applied"
    assert caught.value.entry_id == entry.entry_id
    submitted.assert_awaited_once_with(caught.value.flow_id, config)
    aborted.assert_awaited_once_with(caught.value.flow_id)
    assert entry.options == _SHOWN


@pytest.mark.parametrize("scenario", ["followup_form", "followup_menu"])
async def test_unexpected_followup_does_not_submit_another_restore_step(
    hass: HomeAssistant, client: HomeAssistantClient, scenario: str
) -> None:
    """A form after the one HA called the last, or any menu, is not filled."""
    entry = _entry(hass, scenario)

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        _spy(client, "abort_options_flow") as aborted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(client, entry, {"press": []})

    assert caught.value.apply_status == "not_applied"
    assert caught.value.reason == "unsupported_form"
    assert "authoritative options form" in str(caught.value)
    submitted.assert_awaited_once_with(caught.value.flow_id, {"press": []})
    aborted.assert_awaited_once_with(caught.value.flow_id)
    assert _open_options_flows(hass) == []


# generic_thermostat's options: init, then presets (#2632).
_SNAPSHOT = {"name": "Office", "cold_tolerance": 0.5, "away_temp": 16}


async def test_multi_form_restore_fills_each_form_from_the_snapshot(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass, "two_forms")

    with _spy(client, "submit_options_flow_step") as submitted:
        result = await _restore(
            client, entry, _SNAPSHOT, fixed_options={**_SNAPSHOT, "away_temp": 20}
        )

    assert result["success"] is True
    assert [c.args[1] for c in submitted.await_args_list] == [
        {"cold_tolerance": 0.5},
        {"away_temp": 16},
    ]
    # ``name`` is a fixed option no form offers, not ignored input.
    assert "warnings" not in result


async def test_option_no_form_offers_must_still_match_or_nothing_applies(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A creation-only option (here ``name``) that changed refuses the restore
    before the final form is submitted."""
    entry = _entry(hass, "two_forms")

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        _spy(client, "abort_options_flow") as aborted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(
            client, entry, _SNAPSHOT, fixed_options={**_SNAPSHOT, "name": "Renamed"}
        )

    assert caught.value.reason == "identity_changed"
    assert caught.value.apply_status == "not_applied"
    assert caught.value.fields == ("name",)
    submitted.assert_awaited_once()
    aborted.assert_awaited_once_with(caught.value.flow_id)
    assert _open_options_flows(hass) == []


async def test_native_form_validation_rejection_is_not_applied(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass, "rejected")

    with (
        _spy(client, "submit_options_flow_step") as submitted,
        _spy(client, "abort_options_flow") as aborted,
        pytest.raises(OptionsFlowError) as caught,
    ):
        await _restore(client, entry, {"press": []})

    assert caught.value.apply_status == "not_applied"
    submitted.assert_awaited_once()
    aborted.assert_awaited_once_with(caught.value.flow_id)
    assert entry.options == _SHOWN


async def test_cancellation_after_submit_is_propagated(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass, "hold")

    with _spy(client, "abort_options_flow") as aborted:
        task = asyncio.ensure_future(_restore(client, entry, {"press": []}))
        # Cancel while the submit waits on Core's answer.
        await _Hold.entered.wait()
        task.cancel()
        outcomes = await asyncio.gather(task, return_exceptions=True)
        _Hold.release.set()
        await _Hold.left.wait()

    assert isinstance(outcomes[0], asyncio.CancelledError)
    aborted.assert_not_awaited()
