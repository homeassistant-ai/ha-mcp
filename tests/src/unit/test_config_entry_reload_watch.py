"""What a reconfigure reports about its reload, watched on real Home Assistant Core.

``watch_entry_reload`` subscribes to ``config_entries/subscribe`` on the
server's own client before the submit. Here the reconfigure is a test
integration's own flow, run by Core's flow manager, and every entry fragment
the watch reads is the one Core pushes. How ``ha_set_integration`` turns the
answer into a status is covered in ``test_integration_reconfigure.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import config_entry_reload_watch
from ha_mcp.tools.config_entry_reload_watch import watch_entry_reload

from ._command_faults import pooled_socket, received
from ._ws_transport import abort_transport

DOMAIN = "reload_watch"

# Far below the settle budget a broken early exit would sit through.
PROMPT_S = 5.0


class EntrySetup:
    """The test integration's entry setup: loads, or raises ``error``."""

    def __init__(self) -> None:
        self.error: Exception | None = None

    async def setup_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if self.error is not None:
            raise self.error

    async def unload_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        return None


class _ReconfigureFlow(ConfigFlow):
    VERSION = 1

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=vol.Schema(
                    {vol.Required("host"): str, vol.Required("reload"): bool}
                ),
            )
        entry = self._get_reconfigure_entry()
        data = {"host": user_input["host"]}
        if user_input["reload"]:
            return self.async_update_reload_and_abort(entry, data=data)
        return self.async_update_and_abort(entry, data=data)


@pytest.fixture
def entry_setup(hass: HomeAssistant) -> Iterator[EntrySetup]:
    setup = EntrySetup()
    mock_integration(
        hass,
        MockModule(
            DOMAIN,
            async_setup_entry=setup.setup_entry,
            async_unload_entry=setup.unload_entry,
        ),
    )
    mock_platform(hass, f"{DOMAIN}.config_flow")
    with mock_config_flow(DOMAIN, _ReconfigureFlow):
        yield setup


@pytest.fixture
async def entry_stream(hass: HomeAssistant) -> None:
    """Core's ``config`` integration, which answers ``config_entries/subscribe``."""
    assert await async_setup_component(hass, "config", {})


async def set_up_entry(hass: HomeAssistant, **kwargs: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "old"}, **kwargs)
    entry.add_to_hass(hass)
    if entry.disabled_by is None:
        await hass.config_entries.async_setup(entry.entry_id)
    return entry


async def reconfigure(
    hass: HomeAssistant, entry: ConfigEntry, host: str, *, reload: bool = True
) -> None:
    """Submit the integration's reconfigure flow, as the user's submit does."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": host, "reload": reload}
    )
    assert result["reason"] == "reconfigure_successful"


@pytest.mark.usefixtures("entry_stream")
async def test_a_reconfigure_whose_reload_loads_reports_loaded(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    entry = await set_up_entry(hass)

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "new")
        reloaded = await settled()

    assert reloaded is not None
    assert reloaded["state"] == "loaded"


@pytest.mark.usefixtures("entry_stream")
async def test_a_reconfigure_that_breaks_the_entry_reports_the_reloads_outcome(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """The commit still shows the pre-reload ``loaded``, and the reload passes
    through ``unload_in_progress``, ``not_loaded`` and ``setup_in_progress``:
    taking any of those for the outcome reports a broken entry as fine or
    unverified."""
    entry = await set_up_entry(hass)
    entry_setup.error = ConfigEntryNotReady("host unreachable")

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "unreachable")
        reloaded = await settled()

    assert reloaded is not None
    assert reloaded["state"] == "setup_retry"


@pytest.mark.usefixtures("entry_stream")
async def test_a_state_change_before_the_commit_is_not_the_reconfigures_outcome(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """An entry in ``setup_retry``, the state a user reconfigures to fix, can
    reload on its own mid-flow; that outcome is not the reconfigure's."""
    entry_setup.error = ConfigEntryNotReady("host unreachable")
    entry = await set_up_entry(hass)

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        entry_setup.error = ConfigEntryError("auth rejected")
        assert not await hass.config_entries.async_reload(entry.entry_id)
        entry_setup.error = None
        await reconfigure(hass, entry, "fixed")
        reloaded = await settled()

    assert reloaded is not None
    assert reloaded["state"] == "loaded"


@pytest.mark.usefixtures("entry_stream")
async def test_another_entrys_reload_is_not_taken_for_the_reconfigured_ones(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """The stream carries every entry; the other one's later ``modified_at``
    would pass for this entry's commit."""
    entry = await set_up_entry(hass)
    other = await set_up_entry(hass)

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        entry_setup.error = ConfigEntryError("auth rejected")
        assert not await hass.config_entries.async_reload(other.entry_id)
        entry_setup.error = None
        await reconfigure(hass, entry, "new")
        reloaded = await settled()

    assert reloaded is not None
    assert reloaded["entry_id"] == entry.entry_id
    assert reloaded["state"] == "loaded"


@pytest.mark.usefixtures("entry_stream")
async def test_a_resubmit_that_changes_nothing_gives_up_at_the_commit_budget(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entry_setup: EntrySetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core skips the commit for identical values, so nothing is attributable,
    and waiting out the whole settle budget would only delay that answer."""
    monkeypatch.setattr(config_entry_reload_watch, "_COMMIT_VISIBLE_TIMEOUT", 0.3)
    entry = await set_up_entry(hass)
    loop = asyncio.get_running_loop()

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "old")
        started = loop.time()
        reloaded = await settled()
        elapsed = loop.time() - started

    assert reloaded is None
    assert elapsed < PROMPT_S


@pytest.mark.usefixtures("entry_stream")
async def test_a_commit_with_no_reload_after_it_reads_as_unobserved_once_the_budget_ends(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entry_setup: EntrySetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_reload_watch, "_RELOAD_SETTLE_TIMEOUT", 0.3)
    entry = await set_up_entry(hass)
    loop = asyncio.get_running_loop()

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "new", reload=False)
        started = loop.time()
        reloaded = await settled()
        elapsed = loop.time() - started

    assert reloaded is None
    assert elapsed < PROMPT_S


@pytest.mark.usefixtures("entry_stream")
async def test_a_disabled_entry_reads_as_unobserved_without_waiting(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """Core never reloads a disabled entry, so no transition is coming."""
    entry = await set_up_entry(hass, disabled_by=ConfigEntryDisabler.USER)
    loop = asyncio.get_running_loop()

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "new")
        started = loop.time()
        reloaded = await settled()
        elapsed = loop.time() - started

    assert reloaded is None
    assert elapsed < PROMPT_S


@pytest.mark.usefixtures("entry_stream")
async def test_an_entry_disabled_after_the_commit_settles_without_waiting(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """A disabled entry stays ``not_loaded``; waiting for it to load would
    stall the reconfigure's answer for the whole settle budget."""
    entry = await set_up_entry(hass)
    loop = asyncio.get_running_loop()

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "new", reload=False)
        await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        started = loop.time()
        reloaded = await settled()
        elapsed = loop.time() - started

    assert reloaded is not None
    assert reloaded["disabled_by"] == "user"
    assert elapsed < PROMPT_S


@pytest.mark.usefixtures("entry_stream")
async def test_a_connection_lost_after_the_subscribe_reads_as_unobserved_not_an_error(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """Core has committed the change by then; a stream failure must not hide it."""
    entry = await set_up_entry(hass)

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        abort_transport(await pooled_socket(core_client))
        await reconfigure(hass, entry, "new")
        assert await settled() is None


@pytest.mark.usefixtures("entry_stream")
async def test_the_entry_subscription_is_released_however_the_submit_ends(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """An unreleased subscription stays open on the pooled socket and is
    pushed every later entry change."""
    entry = await set_up_entry(hass)

    async def failing_submit() -> None:
        async with watch_entry_reload(core_client, entry.entry_id):
            raise ValueError("the submit failed")

    with received(hass, "config_entries/subscribe", "unsubscribe_events") as seen:
        async with watch_entry_reload(core_client, entry.entry_id):
            await reconfigure(hass, entry, "new")
        with pytest.raises(ValueError, match="the submit failed"):
            await failing_submit()

    subscribed = [message["id"] for message in seen["config_entries/subscribe"]]
    assert len(subscribed) == 2
    assert [m["subscription"] for m in seen["unsubscribe_events"]] == subscribed


async def test_without_the_entry_stream_the_reload_reads_as_unobserved(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry_setup: EntrySetup
) -> None:
    """Core without ``config`` refuses ``config_entries/subscribe``; the
    reconfigure still goes through."""
    entry = await set_up_entry(hass)

    async with watch_entry_reload(core_client, entry.entry_id) as settled:
        await reconfigure(hass, entry, "new")
        assert await settled() is None
