"""``ha_manage_updates`` on a real ``hass``.

Updates are entities of Core's ``update`` integration (``_tools_updates_core``);
what the tool reads from GitHub, the Home Assistant blog and PyPI is served by
respx.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohasupervisor.models import UpdateChannel
from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import update_check
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT
from ha_mcp.tools.helpers import core_error_message

from ._tool_errors import error_body, error_of
from ._tools_addons_core import SLUG, Route, Supervisor, app_info
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
from ._tools_updates_core import (
    CORE_UPDATE,
    SoftwareUpdate,
    Web,
    manage_updates,
    set_up_platforms,
    set_up_updates,
    supervisor_info,
)
from ._tools_updates_core import web as web

INSTALL = UpdateEntityFeature.INSTALL
BACKUP = UpdateEntityFeature.BACKUP
RELEASE_URL = "https://github.com/owner/repo/releases/tag/v1.0.0"
CORE_TITLE = "Home Assistant Core"


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


async def core_refusal(hass: HomeAssistant, service: str, **data: Any) -> str:
    """The text Core refuses ``update.<service>`` with, called in process."""
    with pytest.raises(HomeAssistantError) as refusal:
        await hass.services.async_call("update", service, data, blocking=True)
    return str(refusal.value)


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


SERVER_APP = "HA-MCP Server"
SERVER_APP_UPDATE = "update.ha_mcp_server_update"
OTHER_APP_UPDATE = "update.mosquitto_broker_update"


async def server_and_other_app(
    hass: HomeAssistant, supervisor: Supervisor
) -> tuple[SoftwareUpdate, SoftwareUpdate]:
    """The update of the app this server runs in and another app's, with
    Supervisor naming this app ``SERVER_APP``."""
    supervisor.answer("GET", "/addons/self/info", app_info(SLUG, name=SERVER_APP))
    own = SoftwareUpdate(SERVER_APP_UPDATE, title=SERVER_APP, features=INSTALL)
    other = SoftwareUpdate(OTHER_APP_UPDATE, title="Mosquitto broker", features=INSTALL)
    await set_up_updates(hass, own, other, integration="hassio")
    return own, other


class TestServerOwnApp:
    """Installing the app this server runs in restarts the server, so the call
    that installs it never gets its answer."""

    async def test_in_the_app_list_marks_the_server_app_and_warns(
        self,
        hass: HomeAssistant,
        app_route: Route,
        component_core_client: HomeAssistantClient,
    ) -> None:
        await server_and_other_app(hass, app_route.supervisor)

        result = await manage_updates(component_core_client)

        marked = {u["entity_id"]: u.get("runs_this_server") for u in result["updates"]}
        assert marked == {SERVER_APP_UPDATE: True, OTHER_APP_UPDATE: None}
        [warning] = result["warnings"]
        assert SERVER_APP_UPDATE in warning

    async def test_outside_the_app_nothing_is_marked_and_supervisor_is_not_asked(
        self,
        hass: HomeAssistant,
        core_supervisor: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        await server_and_other_app(hass, core_supervisor)

        result = await manage_updates(component_core_client)

        assert not any("runs_this_server" in u for u in result["updates"])
        assert "warnings" not in result
        assert ("GET", "/addons/self/info") not in core_supervisor.requests

    async def test_a_batch_install_sends_the_server_app_last(
        self,
        hass: HomeAssistant,
        app_route: Route,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """Sent first, its restart would cut off the installs after it."""
        own, other = await server_and_other_app(hass, app_route.supervisor)
        installed: list[str] = []
        own.on_install = lambda: installed.append(SERVER_APP_UPDATE)
        other.on_install = lambda: installed.append(OTHER_APP_UPDATE)

        result = await manage_updates(
            component_core_client,
            action="install",
            entity_ids=[SERVER_APP_UPDATE, OTHER_APP_UPDATE],
        )

        assert installed == [OTHER_APP_UPDATE, SERVER_APP_UPDATE]
        assert any(SERVER_APP_UPDATE in w for w in result["warnings"])

    async def test_an_unreadable_app_identity_marks_nothing_and_warns_nothing(
        self,
        hass: HomeAssistant,
        app_route: Route,
        component_core_client: HomeAssistantClient,
    ) -> None:
        await set_up_updates(
            hass,
            SoftwareUpdate(SERVER_APP_UPDATE, title=SERVER_APP),
            integration="hassio",
        )
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
            # A Core update whose entity_id was renamed is still the Core update.
            pytest.param("hassio", "Home Assistant Core", None, "core", id="core"),
            pytest.param(
                "hassio", "Home Assistant Operating System", None, "os", id="os"
            ),
            pytest.param(
                "hassio", "Home Assistant Supervisor", None, "supervisor", id="sv"
            ),
            pytest.param("hassio", "Mosquitto broker", None, "addons", id="app"),
            pytest.param("marketplace", "Some card", None, "hacs", id="store"),
            pytest.param(
                "test", "Bulb", UpdateDeviceClass.FIRMWARE, "devices", id="firmware"
            ),
            pytest.param("test", "Home Assistant Core", None, "other", id="other"),
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


class TestBatchActions:
    """install / skip act on each target and report every outcome."""

    async def test_category_install_leaves_out_an_update_core_cannot_install(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        installable = SoftwareUpdate("update.card_a", features=INSTALL)
        notify_only = SoftwareUpdate("update.card_b")
        await set_up_updates(hass, installable, notify_only, integration="marketplace")

        result = await manage_updates(
            component_core_client, action="install", categories=["hacs"]
        )

        assert installable.installs == [(None, False)]
        assert (result["requested"], result["succeeded"], result["failed"]) == (1, 1, 0)

    async def test_without_the_component_category_install_lets_core_refuse(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Which update can be installed is unknown without the component, so
        each is sent and Core's refusal is that item's error."""
        installable = SoftwareUpdate("update.card_a", features=INSTALL)
        notify_only = SoftwareUpdate("update.card_b")
        await set_up_updates(hass, installable, notify_only, integration="marketplace")

        result = await manage_updates(
            core_client, action="install", categories=["hacs"]
        )

        assert installable.installs == [(None, False)]
        assert (result["requested"], result["succeeded"], result["failed"]) == (2, 1, 1)
        [failure] = [r for r in result["results"] if not r["success"]]
        assert failure["entity_id"] == "update.card_b"
        # Core's WebSocket API puts "Validation error: " before this refusal.
        assert (
            await core_refusal(hass, "install", entity_id="update.card_b")
            in (failure["error"]["message"])
        )

    async def test_backup_reaches_only_updates_that_support_it(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        app = SoftwareUpdate(
            "update.app", features=INSTALL | UpdateEntityFeature.BACKUP
        )
        card = SoftwareUpdate("update.card", features=INSTALL)
        await set_up_updates(hass, app, card)

        result = await manage_updates(
            component_core_client,
            action="install",
            entity_ids=["update.app", "update.card"],
            backup=True,
        )

        assert (app.installs, card.installs) == ([(None, True)], [(None, False)])
        assert [(r["entity_id"], r["backup"]) for r in result["results"]] == [
            ("update.app", True),
            ("update.card", False),
        ]

    async def test_chosen_version_reaches_the_update(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        update = SoftwareUpdate(
            "update.thing", features=INSTALL | UpdateEntityFeature.SPECIFIC_VERSION
        )
        await set_up_updates(hass, update)

        await manage_updates(
            core_client, action="install", entity_ids=["update.thing"], version="1.0.5"
        )

        assert update.installs == [("1.0.5", False)]

    @pytest.mark.parametrize(
        ("features", "in_progress", "version"),
        [
            pytest.param(INSTALL, False, "1.0.5", id="no-specific-version"),
            pytest.param(
                INSTALL | UpdateEntityFeature.PROGRESS, True, None, id="in-progress"
            ),
        ],
    )
    async def test_core_refusal_text_reaches_the_caller(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        features: UpdateEntityFeature,
        in_progress: bool,
        version: str | None,
    ) -> None:
        """Core's REST service endpoint answered these with a bare 500."""
        update = SoftwareUpdate(
            "update.thing", features=features, in_progress=in_progress
        )
        await set_up_updates(hass, update)

        result = await manage_updates(
            core_client, action="install", entity_ids=["update.thing"], version=version
        )

        [failure] = result["results"]
        refusal = {"entity_id": "update.thing"}
        if version is not None:
            refusal["version"] = version
        assert failure["error"]["message"] == await core_refusal(
            hass, "install", **refusal
        )
        assert update.installs == []

    async def test_version_cannot_be_sent_to_a_whole_category(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await manage_updates(
                core_client, action="install", categories=["hacs"], version="1.0.5"
            )

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_skip_reports_a_missing_entity_and_skips_the_rest(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.exists"))

        result = await manage_updates(
            core_client, action="skip", entity_ids=["update.exists", "update.missing"]
        )

        state = hass.states.get("update.exists")
        assert state is not None
        assert state.attributes["skipped_version"] == "1.1.0"
        assert (result["requested"], result["succeeded"], result["failed"]) == (2, 1, 1)
        assert result["success"] is False
        [failure] = [r for r in result["results"] if not r.get("success")]
        assert failure["entity_id"] == "update.missing"
        assert failure["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_an_update_core_refuses_does_not_stop_the_batch(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(
            hass,
            SoftwareUpdate("update.auto", auto_update=True),
            SoftwareUpdate("update.manual"),
        )

        result = await manage_updates(
            core_client, action="skip", entity_ids=["update.auto", "update.manual"]
        )

        state = hass.states.get("update.manual")
        assert state is not None
        assert state.attributes["skipped_version"] == "1.1.0"
        assert (result["requested"], result["succeeded"], result["failed"]) == (2, 1, 1)
        [failure] = [r for r in result["results"] if not r.get("success")]
        assert failure["entity_id"] == "update.auto"
        assert failure["error"]["code"] == "SERVICE_CALL_FAILED"

    async def test_an_install_that_outlasts_the_request_is_reported_as_running(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A Core update takes minutes: the call ends without Home Assistant's
        answer, which is not a failure and must not be sent again."""
        slow = SoftwareUpdate("update.slow", features=INSTALL)
        slow.held = asyncio.Event()
        queued = SoftwareUpdate("update.queued", features=INSTALL)
        await set_up_updates(hass, slow, queued)
        core_client.timeout = 1

        try:
            result = await manage_updates(
                core_client,
                action="install",
                entity_ids=["update.slow", "update.queued"],
            )
        finally:
            slow.held.set()
            await hass.async_block_till_done()

        [item] = result["results"]
        assert (item["entity_id"], item["success"], item["partial"]) == (
            "update.slow",
            True,
            True,
        )
        assert result["partial"] is True
        assert result["not_started"] == ["update.queued"]
        assert (result["succeeded"], result["failed"]) == (0, 0)
        assert queued.installs == []

    async def test_lost_connection_ends_the_batch_with_a_connection_error(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(
            hass, SoftwareUpdate("update.first"), SoftwareUpdate("update.second")
        )
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
            ) as send,
            pytest.raises(ToolError) as exc_info,
        ):
            await manage_updates(
                core_client, action="skip", entity_ids=["update.first", "update.second"]
            )

        assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
        sent = [c.args[0]["type"] for c in send.await_args_list]
        assert sent.count("call_service") == 1


@pytest.fixture(params=[False, True], ids=["without-component", "with-component"])
async def either_client(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> HomeAssistantClient:
    """``core_client``, and the same with the component's commands registered."""
    if request.param:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    return core_client


async def test_get_names_an_update_entity_core_does_not_have(
    hass: HomeAssistant, either_client: HomeAssistantClient
) -> None:
    await set_up_updates(hass, SoftwareUpdate("update.exists"))

    with pytest.raises(ToolError) as exc_info:
        await manage_updates(either_client, action="get", entity_ids=["update.missing"])

    error = error_of(exc_info)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert error["message"] == "Update entity not found: update.missing"


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

    async def test_a_restored_update_is_not_asked_to_check(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Its integration is not loaded; the Updates page skips it too."""
        assert await async_setup_component(hass, "homeassistant", {})
        await set_up_updates(hass, SoftwareUpdate("update.loaded"))
        entity_registry.async_get_or_create(
            "update", "test", "gone", suggested_object_id="gone"
        ).write_unavailable_state(hass)

        result = await manage_updates(core_client, action="refresh")

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


async def updates_with_backup(
    hass: HomeAssistant, options: dict[str, bool]
) -> tuple[SoftwareUpdate, SoftwareUpdate, SoftwareUpdate]:
    """A Core, an app and a firmware update that can back up first, on a Home
    Assistant whose backup-before-update settings are ``options``."""
    core = SoftwareUpdate(CORE_UPDATE, title=CORE_TITLE, features=INSTALL | BACKUP)
    app = SoftwareUpdate(
        "update.mosquitto_update", title="Mosquitto broker", features=INSTALL | BACKUP
    )
    firmware = SoftwareUpdate(
        "update.bulb_firmware",
        device_class=UpdateDeviceClass.FIRMWARE,
        features=INSTALL | BACKUP,
    )
    await set_up_platforms(hass, {"hassio": [core, app], "test": [firmware]})
    MockConfigEntry(domain="hassio", options=options).add_to_hass(hass)
    return core, app, firmware


class TestBackupSetting:
    """Without ``backup``, an install follows Home Assistant's own
    backup-before-update setting, as the update dialog's toggle does."""

    @pytest.mark.parametrize(
        ("options", "backed_up"),
        [
            pytest.param(
                {"core_backup_before_update": True}, (True, False, False), id="core"
            ),
            pytest.param(
                {"add_on_backup_before_update": True}, (False, True, False), id="app"
            ),
        ],
    )
    async def test_each_update_follows_the_setting_for_its_kind(
        self,
        hass: HomeAssistant,
        core_supervisor: Supervisor,
        component_core_client: HomeAssistantClient,
        options: dict[str, bool],
        backed_up: tuple[bool, bool, bool],
    ) -> None:
        updates = await updates_with_backup(hass, options)

        result = await manage_updates(
            component_core_client,
            action="install",
            entity_ids=[u.entity_id for u in updates],
        )

        assert tuple(u.installs[0][1] for u in updates) == backed_up
        assert tuple(r["backup"] for r in result["results"]) == backed_up

    async def test_backup_false_overrides_a_setting_that_is_on(
        self,
        hass: HomeAssistant,
        core_supervisor: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        core, app, _ = await updates_with_backup(
            hass,
            {"core_backup_before_update": True, "add_on_backup_before_update": True},
        )

        await manage_updates(
            component_core_client,
            action="install",
            entity_ids=[core.entity_id, app.entity_id],
            backup=False,
        )

        assert (core.installs, app.installs) == ([(None, False)], [(None, False)])

    async def test_an_unreadable_setting_asks_for_no_backup_and_says_so(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        core = SoftwareUpdate(CORE_UPDATE, title=CORE_TITLE, features=INSTALL | BACKUP)
        await set_up_updates(hass, core, integration="hassio")

        result = await manage_updates(
            component_core_client, action="install", entity_ids=[CORE_UPDATE]
        )

        assert core.installs == [(None, False)]
        assert result["warnings"]


class TestInstallTarget:
    """An install asks for the version the caller saw and reports what Home
    Assistant shows after it."""

    async def test_an_install_asks_for_the_version_the_caller_saw(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Otherwise a version published meanwhile would be installed."""
        pinned = SoftwareUpdate(
            "update.pinned",
            latest="2.0.0",
            features=INSTALL | UpdateEntityFeature.SPECIFIC_VERSION,
        )
        latest_only = SoftwareUpdate(
            "update.latest_only", latest="2.0.0", features=INSTALL
        )
        await set_up_updates(hass, pinned, latest_only)

        result = await manage_updates(
            component_core_client,
            action="install",
            entity_ids=["update.pinned", "update.latest_only"],
        )

        assert (pinned.installs, latest_only.installs) == (
            [("2.0.0", False)],
            [(None, False)],
        )
        assert [r.get("version") for r in result["results"]] == ["2.0.0", None]

    async def test_a_skip_reports_the_versions_read_back(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.thing", latest="2.0.0"))

        result = await manage_updates(
            component_core_client, action="skip", entity_ids=["update.thing"]
        )

        [item] = result["results"]
        assert (
            item.items()
            >= {
                "installed_version": "1.0.0",
                "latest_version": "2.0.0",
                "skipped_version": "2.0.0",
                "in_progress": False,
            }.items()
        )

    async def test_an_install_read_back_as_finished_needs_no_confirming(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.thing", features=INSTALL))

        result = await manage_updates(
            component_core_client, action="install", entity_ids=["update.thing"]
        )

        [item] = result["results"]
        assert item["installed_version"] == item["latest_version"]
        assert "note" not in result

    async def test_a_failed_readback_keeps_the_results_and_warns(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        update = SoftwareUpdate("update.thing", features=INSTALL)
        await set_up_updates(hass, update)

        with ExitStack() as lost:

            def lose_connection() -> None:
                lost.enter_context(
                    patch.object(
                        core_client,
                        "get_states",
                        AsyncMock(
                            side_effect=HomeAssistantConnectionError("connection lost")
                        ),
                    )
                )

            update.on_install = lose_connection
            result = await manage_updates(
                core_client, action="install", entity_ids=["update.thing"]
            )

        [item] = result["results"]
        assert item["success"] is True
        assert any("connection lost" in w for w in result["warnings"])
        assert "update.thing" in result["note"]


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
        core_supervisor: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        core_supervisor.answer(
            "GET", "/supervisor/info", supervisor_info(UpdateChannel.BETA)
        )
        await set_up_updates(
            hass, SoftwareUpdate(CORE_UPDATE, title=CORE_TITLE), integration="hassio"
        )

        result = await manage_updates(component_core_client)

        assert result["channel"] == "beta"

    async def test_without_a_supervisor_list_has_no_channel_and_no_warning(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(
            hass, SoftwareUpdate(CORE_UPDATE, title=CORE_TITLE), integration="hassio"
        )

        result = await manage_updates(component_core_client)

        assert "channel" not in result
        assert "warnings" not in result
