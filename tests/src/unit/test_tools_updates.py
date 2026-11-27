"""``ha_manage_updates`` on a real ``hass``: list, get, refresh and the release
channel; the batch actions are in ``test_tools_updates_batch``.

Updates are entities of Core's ``update`` integration (``_tools_updates_core``);
what the tool reads from GitHub, the Home Assistant blog and PyPI is served by
respx.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohasupervisor.models import UpdateChannel
from homeassistant.components.hassio.entity import (
    HassioAddonEntity,
    HassioCoreEntity,
    HassioOSEntity,
    HassioSupervisorEntity,
)
from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp import update_check
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT
from ha_mcp.tools.helpers import core_error_message

from ._hacs_marketplace import set_up_marketplace, stored
from ._hassio_updates_core import (
    OTHER_APP,
    OTHER_SLUG,
    hassio_entity,
    offer_core_update,
)
from ._hassio_updates_core import core_versions as core_versions
from ._hassio_updates_core import hassio_updates as hassio_updates
from ._tool_errors import error_body, error_of
from ._tools_addons_core import Route, Supervisor, app_info, timers
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
from ._tools_updates_core import (
    SoftwareUpdate,
    Web,
    manage_updates,
    restore_update,
    set_up_updates,
    supervisor_info,
)
from ._tools_updates_core import either_client as either_client
from ._tools_updates_core import web as web

INSTALL = UpdateEntityFeature.INSTALL
RELEASE_URL = "https://github.com/owner/repo/releases/tag/v1.0.0"


@pytest.fixture(autouse=True)
def no_self_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's own PyPI check stays off unless a test turns it on."""
    monkeypatch.setenv(update_check.DISABLE_ENV, "1")


async def listed(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    update: SoftwareUpdate,
    integration: str = "test",
) -> dict[str, Any]:
    """The one listed update, as ``list`` reports it."""
    await set_up_updates(hass, update, integration=integration)
    result = await manage_updates(client)
    [entry] = result["updates"]
    return dict(entry)


class TestServerOwnUpdate:
    """``list`` also tells whether a newer ha-mcp is out."""

    async def test_newer_server_release_is_reported(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in (update_check.DISABLE_ENV, "SUPERVISOR_TOKEN", "HA_MCP_EMBEDDED"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.8.0")
        web.serve(
            "https://pypi.org/pypi/ha-mcp/json", json={"info": {"version": "7.9.0"}}
        )
        await set_up_updates(hass)

        result = await manage_updates(core_client)

        assert result["ha_mcp_update"] == {
            "current": "7.8.0",
            "latest": "7.9.0",
            "update_available": True,
        }

    async def test_no_server_update_field_when_the_check_is_off(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass)

        result = await manage_updates(core_client)

        assert "ha_mcp_update" not in result


class TestServerOwnApp:
    """Installing the app this server runs in restarts the server, so the call
    that installs it never gets its answer."""

    async def test_in_the_app_list_marks_the_server_app_and_warns(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        app_route: Route,
        component_core_client: HomeAssistantClient,
    ) -> None:
        hassio_updates.answer(
            "GET", "/addons/self/info", app_info(OTHER_SLUG, name=OTHER_APP)
        )
        own = hassio_entity(hass, HassioAddonEntity, OTHER_APP).entity_id

        result = await manage_updates(component_core_client)

        marked = {u["entity_id"]: u.get("runs_this_server") for u in result["updates"]}
        assert [eid for eid, mark in marked.items() if mark] == [own]
        [warning] = result["warnings"]
        assert own in warning

    async def test_outside_the_app_nothing_is_marked_and_supervisor_is_not_asked(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        result = await manage_updates(component_core_client)

        assert not any("runs_this_server" in u for u in result["updates"])
        assert "warnings" not in result
        assert ("GET", "/addons/self/info") not in hassio_updates.requests

    async def test_an_unreadable_app_identity_marks_nothing_and_warns_nothing(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        app_route: Route,
        component_core_client: HomeAssistantClient,
    ) -> None:
        app_route.supervisor.refuse("GET", "/addons/self/info", "Unknown app")

        result = await manage_updates(component_core_client)

        assert not any("runs_this_server" in u for u in result["updates"])
        assert "warnings" not in result
        assert ("GET", "/addons/self/info") in app_route.supervisor.requests


class TestCategories:
    """Each pending update is filed under the category the Updates page uses,
    read from the integration that owns it and Core's attributes, never from
    its entity_id."""

    @pytest.mark.parametrize(
        ("integration", "title", "device_class", "category"),
        [
            pytest.param(
                "test", "Bulb", UpdateDeviceClass.FIRMWARE, "devices", id="firmware"
            ),
            pytest.param("test", "Bulb hub", None, "other", id="other"),
        ],
    )
    async def test_update_lands_in_its_category(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        integration: str,
        title: str,
        device_class: UpdateDeviceClass | None,
        category: str,
    ) -> None:
        update = SoftwareUpdate(
            "update.renamed_by_user", title=title, device_class=device_class
        )

        entry = await listed(hass, core_client, update, integration)

        assert (entry["platform"], entry["category"]) == (integration, category)

    async def test_a_store_repository_update_lands_under_the_store(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
    ) -> None:
        """An installed repository Core's Marketplace offers a newer release of."""
        card = stored(
            "someone/some-card", "plugin", installed="v1.0.0", last_version="v1.1.0"
        )
        await set_up_marketplace(hass, aioclient_mock, hass_storage, {"1": card})
        assert await async_setup_component(hass, "config", {})

        result = await manage_updates(core_client)

        [entry] = result["updates"]
        assert (entry["platform"], entry["category"]) == ("marketplace", "hacs")

    @pytest.mark.parametrize(
        ("kind", "title", "category"),
        [
            pytest.param(HassioCoreEntity, None, "core", id="core"),
            pytest.param(HassioOSEntity, None, "os", id="os"),
            pytest.param(HassioSupervisorEntity, None, "supervisor", id="sv"),
            pytest.param(HassioAddonEntity, OTHER_APP, "addons", id="app"),
        ],
    )
    async def test_a_supervisor_update_lands_in_its_category(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        either_client: HomeAssistantClient,
        web: Web,
        kind: type,
        title: str | None,
        category: str,
    ) -> None:
        entity_id = hassio_entity(hass, kind, title).entity_id

        result = await manage_updates(
            either_client, action="get", entity_ids=[entity_id]
        )

        assert (result["platform"], result["category"]) == ("hassio", category)

    async def test_with_the_component_supervisor_updates_go_by_their_core_entity(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """An app named like the Core update is still an app, and is not
        taken for the Core update a release analysis is read for."""
        core_title = hassio_entity(hass, HassioCoreEntity).title
        app = hassio_entity(hass, HassioAddonEntity, core_title).entity_id

        result = await manage_updates(
            component_core_client, action="get", entity_ids=[app]
        )

        assert result["category"] == "addons"


class TestUpdateRows:
    """A listed update is Core's state, with what the component reads from
    Core's entity added."""

    async def test_every_attribute_core_wrote_reaches_the_row(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        update = SoftwareUpdate("update.thing", release_url=RELEASE_URL)

        entry = await listed(hass, core_client, update)

        state = hass.states.get("update.thing")
        assert state is not None
        assert entry.items() >= dict(state.attributes).items()

    async def test_features_are_named_after_core_feature_flags(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        update = SoftwareUpdate(
            "update.thing",
            features=INSTALL
            | UpdateEntityFeature.SPECIFIC_VERSION
            | UpdateEntityFeature.RELEASE_NOTES,
        )

        entry = await listed(hass, component_core_client, update)

        assert entry["features"] == ["install", "specific_version", "release_notes"]

    @pytest.mark.parametrize(
        ("features", "in_progress", "can_install"),
        [
            pytest.param(INSTALL, False, True, id="installable"),
            pytest.param(UpdateEntityFeature(0), False, False, id="no-install"),
            pytest.param(
                INSTALL | UpdateEntityFeature.PROGRESS, True, False, id="busy"
            ),
            # Core refuses an install unless in_progress is False.
            pytest.param(
                INSTALL | UpdateEntityFeature.PROGRESS, None, False, id="unknown"
            ),
        ],
    )
    async def test_can_install_follows_core_feature_flags_and_progress(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        features: UpdateEntityFeature,
        in_progress: bool | None,
        can_install: bool,
    ) -> None:
        update = SoftwareUpdate(
            "update.thing", features=features, in_progress=in_progress
        )

        entry = await listed(hass, component_core_client, update)

        assert entry["can_install"] is can_install

    async def test_without_the_component_features_are_unknown_and_said_so(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.thing", features=INSTALL))

        result = await manage_updates(core_client)

        [entry] = result["updates"]
        assert (entry["features"], entry["can_install"]) == (None, None)
        assert any(COMPONENT_INSTALL_HINT in w for w in result["warnings"])

    async def test_get_returns_the_row_list_returns(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.thing", features=INSTALL))
        [entry] = (await manage_updates(component_core_client))["updates"]

        result = await manage_updates(
            component_core_client, action="get", entity_ids=["update.thing"]
        )

        assert result.items() >= entry.items()

    async def test_a_restored_update_is_read_as_core_shows_it(
        self,
        hass: HomeAssistant,
        either_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Its integration did not set it up again; Home Assistant still has it."""
        await set_up_updates(hass)
        entity_id = restore_update(hass, entity_registry)

        result = await manage_updates(
            either_client, action="get", entity_ids=[entity_id]
        )

        state = hass.states.get(entity_id)
        assert state is not None
        assert result.items() >= dict(state.attributes).items()
        assert (result["state"], result["platform"]) == ("unavailable", "test")


async def test_get_names_an_update_entity_core_does_not_have(
    hass: HomeAssistant, either_client: HomeAssistantClient
) -> None:
    await set_up_updates(hass, SoftwareUpdate("update.exists"))

    with pytest.raises(ToolError) as exc_info:
        await manage_updates(either_client, action="get", entity_ids=["update.missing"])

    error = error_of(exc_info)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert error["message"] == "Update entity not found: update.missing"


async def answered(
    hass: HomeAssistant, call: asyncio.Task[Any], besides: set[asyncio.TimerHandle]
) -> None:
    """Return once Core has answered the refresh's call: the tool is done, or
    it waits on a timer due in about 15 seconds."""
    for _ in range(10_000):
        due = [h.when() - hass.loop.time() for h in timers(hass) - besides]
        if call.done() or any(14 < seconds <= 15 for seconds in due):
            return
        await asyncio.sleep(0)
    raise AssertionError("the refresh neither finished nor waited")


class TestRefresh:
    """``refresh`` has every update checked, as the Updates page's "Check for
    updates" does, then lists what Home Assistant offers afterwards."""

    async def test_every_update_is_checked_and_the_new_offer_is_listed(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "homeassistant", {})
        first, second = SoftwareUpdate("update.first"), SoftwareUpdate("update.second")
        first.next_latest = "1.2.0"
        await set_up_updates(hass, first, second)

        result = await manage_updates(core_client, action="refresh")

        assert (first.checks, second.checks) == (1, 1)
        offered = {u["entity_id"]: u["latest_version"] for u in result["updates"]}
        assert offered["update.first"] == "1.2.0"

    async def test_a_version_supervisor_finds_on_its_delayed_check_is_listed(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        either_client: HomeAssistantClient,
    ) -> None:
        """Supervisor's updates check after their coordinator's debounce, once
        Core has answered the call; listed at once, the new Core is missed."""
        assert await async_setup_component(hass, "homeassistant", {})
        await offer_core_update(hass, hassio_updates, "2026.12.0")
        core = hassio_entity(hass, HassioCoreEntity).entity_id
        besides = timers(hass)

        call = asyncio.create_task(manage_updates(either_client, action="refresh"))
        try:
            await answered(hass, call, besides)
            # Home Assistant's clock passes Supervisor's debounce, then the wait.
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
            await hass.async_block_till_done()
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))
            result = await call
        finally:
            if not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)

        offered = {u["entity_id"]: u["latest_version"] for u in result["updates"]}
        assert offered.get(core) == "2026.12.0"

    async def test_a_restored_update_is_not_asked_to_check(
        self,
        hass: HomeAssistant,
        either_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Its integration is not loaded; the Updates page skips it too."""
        assert await async_setup_component(hass, "homeassistant", {})
        await set_up_updates(hass, SoftwareUpdate("update.loaded"))
        restore_update(hass, entity_registry)

        result = await manage_updates(either_client, action="refresh")

        assert result["refreshed"] == 1

    async def test_with_no_update_entities_nothing_is_asked(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass)
        with patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(wraps=core_client.send_websocket_message),
        ) as send:
            result = await manage_updates(core_client, action="refresh")

        assert result["refreshed"] == 0
        assert "note" in result
        assert "call_service" not in [c.args[0]["type"] for c in send.await_args_list]

    async def test_a_check_core_refuses_ends_the_call_with_core_answer(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core swallows an integration's failed check, so its refusal here is
        the missing ``homeassistant`` integration."""
        await set_up_updates(hass, SoftwareUpdate("update.thing"))

        with pytest.raises(ToolError) as exc_info:
            await manage_updates(core_client, action="refresh")

        reply = await core_client.send_websocket_message(
            {
                "type": "call_service",
                "domain": "homeassistant",
                "service": "update_entity",
                "service_data": {"entity_id": "update.thing"},
            }
        )
        assert error_body(exc_info)["ha_error_code"] == reply["error_code"]
        assert core_error_message(reply["error"]) in error_of(exc_info)["message"]


class TestReleaseChannel:
    """Supervisor's release channel, as the Updates page's Join beta / Leave
    beta sets it."""

    async def test_set_channel_writes_it_and_returns_the_old_and_new_one(
        self, core_supervisor: Supervisor, core_client: HomeAssistantClient
    ) -> None:
        core_supervisor.answer(
            "GET", "/supervisor/info", supervisor_info(UpdateChannel.STABLE)
        )
        core_supervisor.answer(
            "GET", "/supervisor/info", supervisor_info(UpdateChannel.BETA)
        )
        core_supervisor.answer("POST", "/supervisor/options")
        core_supervisor.answer("POST", "/supervisor/reload")

        result = await manage_updates(core_client, action="set_channel", channel="beta")

        assert core_supervisor.sent("POST", "/supervisor/options") == [
            {"channel": "beta"}
        ]
        assert core_supervisor.requests[1:3] == [
            ("POST", "/supervisor/options"),
            ("POST", "/supervisor/reload"),
        ]
        assert (result["previous_channel"], result["channel"]) == ("stable", "beta")

    async def test_set_channel_without_a_channel_is_refused(
        self, core_supervisor: Supervisor, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await manage_updates(core_client, action="set_channel")

        assert error_of(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
        assert core_supervisor.requests == []

    async def test_supervisor_refusal_of_a_channel_reaches_the_caller(
        self, core_supervisor: Supervisor, core_client: HomeAssistantClient
    ) -> None:
        core_supervisor.answer(
            "GET", "/supervisor/info", supervisor_info(UpdateChannel.STABLE)
        )
        refusal = (
            "expected UpdateChannel or one of 'stable', 'beta', 'dev' for "
            "dictionary value @ data['channel']. Got 'nightly'"
        )
        core_supervisor.refuse("POST", "/supervisor/options", refusal)

        with pytest.raises(ToolError) as exc_info:
            await manage_updates(core_client, action="set_channel", channel="nightly")

        assert refusal in error_of(exc_info)["message"]
        assert ("POST", "/supervisor/reload") not in core_supervisor.requests

    async def test_without_a_supervisor_set_channel_is_refused(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await manage_updates(core_client, action="set_channel", channel="beta")

        assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"

    async def test_list_carries_the_channel_supervisor_reports(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        result = await manage_updates(component_core_client)

        # The channel the fixture's Supervisor reports.
        assert result["channel"] == UpdateChannel.STABLE.value
        assert not any("channel" in w for w in result.get("warnings", []))

    async def test_a_channel_supervisor_refuses_to_report_is_a_warning_with_its_reason(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """A hassio row means a Supervisor runs, so a missing channel is a read
        that failed; the list says so, with Supervisor's reason, instead of
        dropping it silently."""
        hassio_updates.refuse("GET", "/supervisor/info", "Supervisor is starting up")

        result = await manage_updates(component_core_client)

        assert any(row["platform"] == "hassio" for row in result["updates"])
        assert "channel" not in result
        [warning] = [w for w in result["warnings"] if "channel" in w]
        assert warning.startswith("Supervisor's release channel could not be read (")
        assert "Supervisor is starting up" in warning
