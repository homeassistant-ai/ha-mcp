"""The Marketplace config entry's own flows through the store tools: the GitHub
sign-in (its reconfigure flow) and the download source (its options flow).

The Marketplace and its flows are Core's own (``_hacs_marketplace``). GitHub
answers only where a test serves it (``_hacs_actions_core``).
"""

from __future__ import annotations

import asyncio
from asyncio import FIRST_COMPLETED
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.marketplace import (
    async_setup_entry as setup_marketplace,
)
from homeassistant.components.marketplace.const import CONF_PROFILE
from homeassistant.components.marketplace.const import DOMAIN as MARKETPLACE
from homeassistant.components.marketplace.profiles import DEFAULT_PROFILE, PROFILES
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import config_entry_reload_watch
from ha_mcp.tools.tools_hacs import HacsTools

from ._hacs_actions_core import (
    USER_CODE,
    add_connected_marketplace,
    serve_github_sign_in,
)
from ._hacs_marketplace import (
    CARD_ID,
    pooled_socket,
    set_up_marketplace,
    stored,
    suggestions,
)
from ._mock_flow_integration import answer_translations
from ._tool_errors import rejected_call_body

REPOSITORIES = {CARD_ID: stored("piitaya/lovelace-mushroom", "plugin")}
PENDING = {"error": "authorization_pending"}
CONFIRMED = {"access_token": "gho_token", "token_type": "bearer", "scope": ""}


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> HacsTools:
    return HacsTools(core_client)


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.fixture(autouse=True)
async def flows(hass: HomeAssistant) -> None:
    """Core's config-entry flow API and the flow strings the frontend reads,
    which every Home Assistant serves."""
    assert await async_setup_component(hass, "config", {})
    answer_translations(hass)


@pytest.fixture
async def marketplace(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[MockConfigEntry]:
    """The Marketplace, set up by Core with GitHub unreachable."""
    entry = await set_up_marketplace(hass, aioclient_mock, hass_storage, REPOSITORIES)
    try:
        yield entry
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("ha_manage_hacs", {"action": "connect_github"}),
        ("ha_manage_hacs", {"action": "set_download_source"}),
        ("ha_get_hacs_info", {"action": "download_source"}),
    ],
    ids=["connect_github", "set_download_source", "download_source"],
)
async def test_a_flow_action_without_a_marketplace_entry_is_a_missing_component(
    hass: HomeAssistant, tools: HacsTools, tool: str, arguments: dict[str, Any]
) -> None:
    """The flows are the Marketplace entry's; with no entry there is none to
    open, and the answer says how the Marketplace comes."""
    body = await rejected_call_body(getattr(tools, tool)(**arguments))

    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert any("2026.11" in s for s in suggestions(body))
    assert hass.config_entries.async_entries(MARKETPLACE) == []


class TestConnectGitHub:
    """The GitHub sign-in is the Marketplace entry's reconfigure flow, which
    only the tool that opened it can carry on (#3127)."""

    @staticmethod
    async def _signing_in(
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        *token_answers: dict[str, Any],
    ) -> MockConfigEntry:
        """The Marketplace, GitHub answering the device sign-in."""
        serve_github_sign_in(aioclient_mock, *token_answers)
        return await set_up_marketplace(
            hass, aioclient_mock, hass_storage, REPOSITORIES
        )

    @staticmethod
    def _open(hass: HomeAssistant) -> set[str]:
        return {f["flow_id"] for f in hass.config_entries.flow.async_progress()}

    async def test_the_flow_opens_at_its_first_step_and_can_be_ended(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        opened = (await tools.ha_manage_hacs(action="connect_github"))["data"]
        assert (opened["flow_type"], opened["step_id"]) == ("form", "reconfigure")
        assert opened["flow_id"] in self._open(hass)

        ended = await tools.ha_manage_hacs(
            action="connect_github", flow_id=opened["flow_id"], abort=True
        )
        assert ended["data"]["aborted"] is True
        assert opened["flow_id"] not in self._open(hass)

    async def test_an_ended_flow_says_nothing_about_github(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
    ) -> None:
        """Seen live: on an instance GitHub was connected to all along, a call
        with the flow_id of an aborted sign-in answered that GitHub is
        connected, as if that sign-in had finished."""
        entry = add_connected_marketplace(
            hass, aioclient_mock, hass_storage, REPOSITORIES
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        tools = HacsTools(component_core_client)
        try:
            flow_id = await self._flow_id(tools)
            await tools.ha_manage_hacs(
                action="connect_github", flow_id=flow_id, abort=True
            )
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="connect_github", flow_id=flow_id)
            )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert body["flow_id"] == flow_id

    async def _flow_id(self, tools: HacsTools) -> str:
        opened = await tools.ha_manage_hacs(action="connect_github")
        flow_id: str = opened["data"]["flow_id"]
        return flow_id

    async def test_the_code_comes_back_at_once_and_the_outcome_once_entered(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        """The device step answers with GitHub's URL and the code before the
        user entered it; once they did, Core holds the flow at its last step
        until it is read, and the next call reads it, which ends the flow, and
        the outcome from the Marketplace."""
        entry = await self._signing_in(
            hass, aioclient_mock, hass_storage, PENDING, CONFIRMED
        )
        try:
            flow_id = await self._flow_id(tools)
            device = (
                await tools.ha_manage_hacs(action="connect_github", flow_id=flow_id)
            )["data"]
            assert flow_id in self._open(hass)
            # GitHub's interval passes and the code is checked again.
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
            await hass.async_block_till_done()
            outcome = (
                await tools.ha_manage_hacs(action="connect_github", flow_id=flow_id)
            )["data"]
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert (device["flow_type"], device["step_id"]) == ("progress", "device")
        assert device["description_placeholders"]["code"] == USER_CODE
        assert device["description_placeholders"]["url"].startswith("https://")
        assert outcome["github_connected"] is True
        assert flow_id not in self._open(hass)

    async def test_a_code_confirmed_at_once_ends_the_sign_in_in_that_call(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        entry = await self._signing_in(hass, aioclient_mock, hass_storage, CONFIRMED)
        try:
            flow_id = await self._flow_id(tools)
            outcome = (
                await tools.ha_manage_hacs(action="connect_github", flow_id=flow_id)
            )["data"]
            await hass.async_block_till_done()
            info = await socket.send_command("marketplace/info")
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert outcome["github_connected"] is True
        # The flow's end is the answer itself, as every other finished one.
        assert "flow_open" not in outcome
        assert info["result"]["github_connected"] is True

    async def test_the_outcome_waits_for_the_reload_with_the_new_token(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        """Core reloads the Marketplace with the token as the flow ends, and
        it answers nothing until set up again: a status read while that setup
        runs says nothing about the sign-in."""
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_setup(core: HomeAssistant, entry: MockConfigEntry) -> Any:
            entered.set()
            await release.wait()
            return await setup_marketplace(core, entry)

        entry = await self._signing_in(hass, aioclient_mock, hass_storage, CONFIRMED)
        try:
            flow_id = await self._flow_id(tools)
            with patch(
                "homeassistant.components.marketplace.async_setup_entry",
                side_effect=held_setup,
            ):
                call = asyncio.create_task(
                    tools.ha_manage_hacs(action="connect_github", flow_id=flow_id)
                )
                reloading = asyncio.ensure_future(entered.wait())
                await asyncio.wait({call, reloading}, return_when=FIRST_COMPLETED)
                reloading.cancel()
                # Time for a call that does not wait to read the Marketplace.
                for _ in range(100):
                    await asyncio.sleep(0)
                waited = not call.done()
                release.set()
                outcome = (await call)["data"]
        finally:
            release.set()
            await hass.config_entries.async_unload(entry.entry_id)

        assert waited
        assert outcome["github_connected"] is True

    async def test_a_reload_that_fails_with_the_new_token_is_named(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        """The Marketplace answers nothing after a reload that failed: the
        call says what the reload ended in, not that it did not answer yet."""
        entry = await self._signing_in(hass, aioclient_mock, hass_storage, CONFIRMED)
        try:
            flow_id = await self._flow_id(tools)
            with patch(
                "homeassistant.components.marketplace.async_setup_entry",
                side_effect=ConfigEntryError("GitHub refused the token"),
            ):
                outcome = await tools.ha_manage_hacs(
                    action="connect_github", flow_id=flow_id
                )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        failed = ConfigEntryState.SETUP_ERROR.value
        assert outcome["data"]["github_connected"] is None
        assert outcome["data"]["entry_state"] == failed
        assert any(failed in w for w in outcome["warnings"])

    async def test_a_sign_in_github_refuses_keeps_cores_reason(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        entry = await self._signing_in(
            hass,
            aioclient_mock,
            hass_storage,
            {"error": "expired_token", "error_description": "The code expired"},
        )
        try:
            flow_id = await self._flow_id(tools)
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="connect_github", flow_id=flow_id)
            )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["details"]["reason"] == "activation_failed"
        assert flow_id not in self._open(hass)


class TestDownloadSource:
    """The panel's Download source is the Marketplace entry's options flow:
    one form whose select names the download profiles, answered one step per
    call as ha_set_integration answers an options flow."""

    @staticmethod
    def _open(hass: HomeAssistant) -> set[str]:
        return {f["flow_id"] for f in hass.config_entries.options.async_progress()}

    async def test_the_read_is_cores_options_form(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        """Core's choices, the saved one as current and Core's help text, from
        a flow that is ended afterwards."""
        mirror = next(p for p in PROFILES if p != DEFAULT_PROFILE)
        hass.config_entries.async_update_entry(
            marketplace, options={CONF_PROFILE: mirror}
        )

        result = (await tools.ha_get_hacs_info(action="download_source"))["data"]

        texts = await async_get_translations(hass, "en", "options", [MARKETPLACE])
        help_key = f"component.{MARKETPLACE}.options.step.init.data_description"
        [field] = result["options_schema"]["fields"]
        assert (field["name"], field["options"]) == (CONF_PROFILE, list(PROFILES))
        assert field["current"] == mirror
        assert field["description"] == texts[f"{help_key}.{CONF_PROFILE}"]
        assert self._open(hass) == set()

    async def test_each_source_carries_cores_label_naming_who_runs_it(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        """A mirror is run by a third party; Core names who in each option's
        label, which the panel shows and the user chooses by."""
        result = (await tools.ha_get_hacs_info(action="download_source"))["data"]

        texts = await async_get_translations(hass, "en", "selector", [MARKETPLACE])
        label_key = f"component.{MARKETPLACE}.selector.{CONF_PROFILE}.options"
        [field] = result["options_schema"]["fields"]
        assert field["option_labels"] == {
            profile: texts[f"{label_key}.{profile}"] for profile in PROFILES
        }

    async def test_the_first_call_returns_cores_form_and_saves_nothing(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        before = dict(marketplace.options)
        opened = (await tools.ha_manage_hacs(action="set_download_source"))["data"]

        assert (opened["flow_type"], opened["step_id"]) == ("form", "init")
        assert [f["name"] for f in opened["fields"]] == [CONF_PROFILE]
        assert opened["flow_id"] in self._open(hass)
        assert dict(marketplace.options) == before

    async def test_a_chosen_source_is_saved_and_read_back_once_reloaded(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        opened = (await tools.ha_manage_hacs(action="set_download_source"))["data"]
        [field] = opened["fields"]
        other = next(o for o in field["options"] if o != field["current"])

        saved = (
            await tools.ha_manage_hacs(
                action="set_download_source",
                flow_id=opened["flow_id"],
                config={CONF_PROFILE: other},
            )
        )["data"]
        read = (await tools.ha_get_hacs_info(action="download_source"))["data"]

        assert marketplace.options[CONF_PROFILE] == other
        assert saved["entry_state"] == ConfigEntryState.LOADED.value
        # The write reads the form back under the read's own name.
        assert saved["options_schema"]["fields"][0]["current"] == other
        assert read["options_schema"]["fields"][0]["current"] == other

    async def test_a_reload_that_leaves_the_marketplace_unloaded_is_reported(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        """Core saves the source and reloads; a setup that then fails leaves
        the Marketplace unloaded, which the call must not pass off as done."""
        opened = (await tools.ha_manage_hacs(action="set_download_source"))["data"]
        [field] = opened["fields"]
        other = next(o for o in field["options"] if o != field["current"])

        with patch(
            "homeassistant.components.marketplace.async_setup_entry",
            side_effect=ConfigEntryError("GitHub refused the download source"),
        ):
            saved = await tools.ha_manage_hacs(
                action="set_download_source",
                flow_id=opened["flow_id"],
                config={CONF_PROFILE: other},
            )

        assert marketplace.state is ConfigEntryState.SETUP_ERROR
        assert saved["data"]["entry_state"] == ConfigEntryState.SETUP_ERROR.value
        assert any(ConfigEntryState.SETUP_ERROR.value in w for w in saved["warnings"])

    async def test_the_source_already_in_use_is_saved_without_a_claimed_reload(
        self,
        hass: HomeAssistant,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core reloads the Marketplace only when the saved options changed:
        the call reports no reload rather than one it never saw."""
        # Shortens the wait for a commit that never comes; any value works.
        monkeypatch.setattr(config_entry_reload_watch, "COMMIT_VISIBLE_TIMEOUT", 0.2)
        hass.config_entries.async_update_entry(
            marketplace, options={CONF_PROFILE: DEFAULT_PROFILE}
        )
        opened = (await tools.ha_manage_hacs(action="set_download_source"))["data"]
        [field] = opened["fields"]

        saved = await tools.ha_manage_hacs(
            action="set_download_source",
            flow_id=opened["flow_id"],
            config={CONF_PROFILE: field["current"]},
        )

        assert marketplace.options[CONF_PROFILE] == field["current"]
        assert saved["data"]["entry_state"] is None
        assert (
            saved["data"]["options_schema"]["fields"][0]["current"]
            == (field["current"])
        )
        assert saved["warnings"]

    async def test_a_source_core_does_not_offer_is_cores_refusal(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        before = dict(marketplace.options)
        body = await rejected_call_body(
            tools.ha_manage_hacs(
                action="set_download_source", config={CONF_PROFILE: "nowhere"}
            )
        )
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert list(body["field_errors"]) == [CONF_PROFILE]
        assert dict(marketplace.options) == before

    async def test_an_aborted_flow_leaves_the_source_alone(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        before = dict(marketplace.options)
        opened = (await tools.ha_manage_hacs(action="set_download_source"))["data"]
        ended = (
            await tools.ha_manage_hacs(
                action="set_download_source", flow_id=opened["flow_id"], abort=True
            )
        )["data"]

        assert ended["aborted"] is True
        assert opened["flow_id"] not in self._open(hass)
        assert dict(marketplace.options) == before
