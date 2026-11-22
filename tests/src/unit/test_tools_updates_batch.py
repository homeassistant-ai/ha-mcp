"""``ha_manage_updates``' batch actions (install, skip, clear_skipped) on a real
``hass``; updates are entities of Core's ``update`` integration
(``_tools_updates_core``)."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.hassio.entity import HassioAddonEntity, HassioCoreEntity
from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import update_check
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.exceptions import HomeAssistantCommandNotSent
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.helpers import core_error_message

from ._hassio_updates_core import (
    OTHER_APP,
    OTHER_SLUG,
    hassio_entity,
    serve_backups,
)
from ._hassio_updates_core import core_versions as core_versions
from ._hassio_updates_core import hassio_updates as hassio_updates
from ._tool_errors import error_of
from ._tools_addons_core import SLUG, Route, Supervisor, app_info
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
from ._tools_updates_core import (
    CORE_UPDATE,
    SoftwareUpdate,
    core_refusal,
    manage_updates,
    restore_update,
    set_up_updates,
)
from ._tools_updates_core import either_client as either_client

INSTALL = UpdateEntityFeature.INSTALL
BACKUP = UpdateEntityFeature.BACKUP
SPECIFIC = UpdateEntityFeature.SPECIFIC_VERSION
CORE_TITLE = "Home Assistant Core"


@pytest.fixture(autouse=True)
def no_self_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's own PyPI check stays off."""
    monkeypatch.setenv(update_check.DISABLE_ENV, "1")


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
        app = SoftwareUpdate("update.app", features=INSTALL | BACKUP)
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

    async def test_without_the_component_a_backup_asked_for_is_sent(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Which update can back up is unknown, so Home Assistant decides."""
        update = SoftwareUpdate("update.app", features=INSTALL | BACKUP)
        await set_up_updates(hass, update)

        await manage_updates(
            core_client, action="install", entity_ids=["update.app"], backup=True
        )

        assert update.installs == [(None, True)]

    async def test_chosen_version_reaches_the_update(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        update = SoftwareUpdate("update.thing", features=INSTALL | SPECIFIC)
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

    @pytest.mark.parametrize(
        "batch",
        [
            pytest.param({"categories": ["hacs"]}, id="categories"),
            pytest.param({"action": "skip", "entity_ids": ["update.x"]}, id="skip"),
        ],
    )
    async def test_version_goes_only_with_an_install_of_named_updates(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        batch: dict[str, object],
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await manage_updates(
                core_client, **{"action": "install", **batch}, version="1.0.5"
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

    async def test_a_write_to_a_restored_update_is_refused(
        self,
        hass: HomeAssistant,
        either_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Core's service skips it and answers success, which would read as done."""
        await set_up_updates(hass)
        entity_id = restore_update(hass, entity_registry)

        result = await manage_updates(
            either_client, action="skip", entity_ids=[entity_id]
        )

        [item] = result["results"]
        assert (item["entity_id"], item["error"]["code"]) == (
            entity_id,
            "ENTITY_UNAVAILABLE",
        )


class TestLostConnection:
    """A batch whose connection to Home Assistant is lost."""

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
        assert "keeps running" in item["message"]
        assert result["partial"] is True
        assert result["not_started"] == ["update.queued"]
        assert (result["succeeded"], result["failed"]) == (0, 0)
        assert queued.installs == []

    async def test_an_install_cut_off_by_a_dropped_connection_is_not_said_to_run(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Home Assistant restarting mid-install may have stopped it; saying it
        keeps running would keep the caller from sending it again."""
        await set_up_updates(hass, SoftwareUpdate("update.thing", features=INSTALL))
        with patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ):
            result = await manage_updates(
                core_client, action="install", entity_ids=["update.thing"]
            )

        [item] = result["results"]
        assert (item["success"], item["partial"]) == (True, True)
        assert "keeps running" not in item["message"]

    async def test_an_install_never_sent_fails_with_a_connection_error(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.thing", features=INSTALL))
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantCommandNotSent("socket not ready")),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await manage_updates(
                core_client, action="install", entity_ids=["update.thing"]
            )

        assert error_of(exc_info)["code"] == "CONNECTION_FAILED"

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


async def test_a_batch_install_sends_the_server_app_last(
    hass: HomeAssistant,
    hassio_updates: Supervisor,
    app_route: Route,
    component_core_client: HomeAssistantClient,
) -> None:
    """Sent first, its restart would cut off the installs after it."""
    hassio_updates.answer(
        "GET", "/addons/self/info", app_info(OTHER_SLUG, name=OTHER_APP)
    )
    own = hassio_entity(hass, HassioAddonEntity, OTHER_APP).entity_id
    core_title = hassio_entity(hass, HassioCoreEntity).title
    other = hassio_entity(hass, HassioAddonEntity, core_title).entity_id

    result = await manage_updates(
        component_core_client, action="install", entity_ids=[own, other]
    )

    updates = [
        path
        for method, path in hassio_updates.requests
        if method == "POST" and path.startswith("/store/addons/")
    ]
    assert updates == [
        f"/store/addons/{SLUG}/update",
        f"/store/addons/{OTHER_SLUG}/update",
    ]
    assert any(own in w for w in result["warnings"])


def set_backup_options(hass: HomeAssistant, options: dict[str, bool]) -> None:
    """Home Assistant's backup-before-update settings, stored on hassio's entry."""
    [entry] = hass.config_entries.async_entries("hassio")
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})


class TestBackupSetting:
    """Without ``backup``, an install follows Home Assistant's own
    backup-before-update setting, as the update dialog of one update does."""

    @pytest.mark.parametrize("core_versions", [("2026.11.1", "2026.11.2")])
    @pytest.mark.parametrize(
        ("option", "backed_up"),
        [
            pytest.param("core_backup_before_update", (True, False, False), id="core"),
            pytest.param("add_on_backup_before_update", (False, True, False), id="app"),
        ],
    )
    async def test_each_update_follows_the_setting_for_its_kind(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        component_core_client: HomeAssistantClient,
        option: str,
        backed_up: tuple[bool, bool, bool],
    ) -> None:
        set_backup_options(hass, {option: True})
        await serve_backups(hass, hassio_updates)
        hassio_updates.answer("POST", "/core/update")
        firmware = SoftwareUpdate(
            "update.bulb_firmware",
            device_class=UpdateDeviceClass.FIRMWARE,
            features=INSTALL | BACKUP,
        )
        await set_up_updates(hass, firmware)
        core = hassio_entity(hass, HassioCoreEntity).entity_id
        app = hassio_entity(hass, HassioAddonEntity, OTHER_APP).entity_id

        result = await manage_updates(
            component_core_client,
            action="install",
            entity_ids=[core, app, firmware.entity_id],
        )

        assert tuple(r["backup"] for r in result["results"]) == backed_up
        assert all(r["success"] for r in result["results"])
        # Core's hassio entity backs up through Supervisor, then installs.
        [backup] = hassio_updates.sent("POST", "/backups/new/partial")
        backed = (
            backup.get("homeassistant") is True,
            OTHER_SLUG in (backup.get("addons") or []),
        )
        assert backed == backed_up[:2]
        app_install = f"/store/addons/{OTHER_SLUG}/update"
        assert [
            path
            for method, path in hassio_updates.requests
            if method == "POST" and path in ("/core/update", app_install)
        ] == ["/core/update", app_install]
        assert firmware.installs == [(None, False)]

    @pytest.mark.parametrize("core_versions", [("2026.11.1", "2026.11.2")])
    async def test_backup_false_overrides_a_setting_that_is_on(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        component_core_client: HomeAssistantClient,
    ) -> None:
        [entry] = hass.config_entries.async_entries("hassio")
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                "core_backup_before_update": True,
                "add_on_backup_before_update": True,
            },
        )
        hassio_updates.answer("POST", "/core/update")
        core = hassio_entity(hass, HassioCoreEntity).entity_id
        app = hassio_entity(hass, HassioAddonEntity, OTHER_APP).entity_id

        result = await manage_updates(
            component_core_client,
            action="install",
            entity_ids=[core, app],
            backup=False,
        )

        assert [(r["success"], r["backup"]) for r in result["results"]] == [
            (True, False),
            (True, False),
        ]
        assert ("POST", "/core/update") in hassio_updates.requests
        assert not any(p.startswith("/backups") for _, p in hassio_updates.requests)

    async def test_an_unreadable_setting_asks_for_no_backup_and_says_why(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Core's hassio integration always answers the setting, so the Core
        update here is the stand-in's."""
        core = SoftwareUpdate(CORE_UPDATE, title=CORE_TITLE, features=INSTALL | BACKUP)
        await set_up_updates(hass, core, integration="hassio")

        result = await manage_updates(
            component_core_client, action="install", entity_ids=[CORE_UPDATE]
        )

        refusal = await component_core_client.send_websocket_message(
            {"type": "hassio/update/config/info"}
        )
        assert core.installs == [(None, False)]
        assert any(
            core_error_message(refusal["error"]) in w for w in result["warnings"]
        )


class TestInstallTarget:
    """An install asks for the version the caller saw and reports what Home
    Assistant shows after it."""

    async def test_an_install_asks_for_the_version_the_caller_saw(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Otherwise a version published meanwhile would be installed."""
        pinned = SoftwareUpdate(
            "update.pinned", latest="2.0.0", features=INSTALL | SPECIFIC
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

    async def test_an_up_to_date_update_is_left_for_core_to_refuse(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Sent its own version, Core would reinstall it; the dialog offers no
        install there."""
        current = SoftwareUpdate(
            "update.current", latest="1.0.0", features=INSTALL | SPECIFIC
        )
        await set_up_updates(hass, current)

        result = await manage_updates(
            component_core_client, action="install", entity_ids=["update.current"]
        )

        [failure] = result["results"]
        assert failure["error"]["message"] == await core_refusal(
            hass, "install", entity_id="update.current"
        )
        assert current.installs == []

    async def test_a_skipped_version_is_still_installed_at_that_version(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """The dialog offers the install of a skipped version too."""
        skipped = SoftwareUpdate(
            "update.skipped", latest="2.0.0", features=INSTALL | SPECIFIC
        )
        await set_up_updates(hass, skipped)
        await hass.services.async_call(
            "update", "skip", {"entity_id": "update.skipped"}, blocking=True
        )

        await manage_updates(
            component_core_client, action="install", entity_ids=["update.skipped"]
        )

        assert skipped.installs == [("2.0.0", False)]

    async def test_a_result_carries_the_whole_row_read_back(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Its progress and every other attribute, not a chosen few."""
        await set_up_updates(hass, SoftwareUpdate("update.thing", latest="2.0.0"))

        result = await manage_updates(
            component_core_client, action="skip", entity_ids=["update.thing"]
        )

        [item] = result["results"]
        state = hass.states.get("update.thing")
        assert state is not None
        assert item.items() >= dict(state.attributes).items()
        assert item["skipped_version"] == "2.0.0"

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
