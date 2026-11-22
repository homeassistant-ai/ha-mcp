"""``ha_manage_updates`` on a real ``hass``.

Updates are entities of Core's ``update`` integration (``_tools_updates_core``);
what the tool reads from GitHub, the Home Assistant blog and PyPI is served by
respx.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ha_mcp import update_check
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT

from ._tool_errors import error_of
from ._tools_updates_core import (
    SoftwareUpdate,
    Web,
    manage_updates,
    set_up_updates,
)
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
        assert result["results"] == [
            {"success": True, "entity_id": "update.app", "backup": True},
            {"success": True, "entity_id": "update.card", "backup": False},
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
