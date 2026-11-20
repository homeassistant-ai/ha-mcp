"""ha_config_set_dashboard's create-or-update path, against Core's own ``lovelace``.

The tool runs on the server's own client connected to a real ``hass``: every
registry row, stored config and refusal is Core's. Core stores a dashboard
under an id slugified from its url_path (``my-dash`` becomes ``my_dash``), and
refuses a new hyphenless url_path unless the creator allows a single word, as
its own onboarding does for ``map``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.const import EVENT_LOVELACE_UPDATED
from homeassistant.core import Event, HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

from ._client_spies import sent_messages
from ._config_dir_yaml import write_config_files
from ._entity_registry_core import sent_types
from ._tool_errors import rejected_call_body

_LOGGER_NAME = "ha_mcp.tools.tools_config_dashboards"
_STRATEGY = {"strategy": {"type": "map"}}


@pytest.fixture
async def lovelace(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})


async def _create(client: HomeAssistantClient, url_path: str, **fields: Any) -> None:
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": "Original",
            **fields,
        }
    )
    assert created["success"], created


async def _save(
    client: HomeAssistantClient, url_path: str, config: dict[str, Any]
) -> None:
    saved = await client.send_websocket_message(
        {"type": "lovelace/config/save", "url_path": url_path, "config": config}
    )
    assert saved["success"], saved


async def _row(client: HomeAssistantClient, url_path: str) -> dict[str, Any]:
    """Core's registry row for ``url_path``."""
    listed = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    rows: list[dict[str, Any]] = listed["result"]
    return next(row for row in rows if row["url_path"] == url_path)


async def _stored(client: HomeAssistantClient, url_path: str | None) -> Any:
    """Core's reply to reading the config of ``url_path`` (``None``: the default)."""
    message: dict[str, Any] = {"type": "lovelace/config", "force": True}
    if url_path is not None:
        message["url_path"] = url_path
    return await client.send_websocket_message(message)


def _set_tool(client: HomeAssistantClient) -> Any:
    return DashboardConfigTools(client).ha_config_set_dashboard


@pytest.fixture
async def my_dashboard(lovelace: None, core_client: HomeAssistantClient) -> None:
    """``my-dashboard``, stored by Core under the id ``my_dashboard``."""
    await _create(core_client, "my-dashboard")


@pytest.fixture
async def floorplan(lovelace: None, core_client: HomeAssistantClient) -> None:
    """``floorplan``, a hyphenless dashboard holding an empty config."""
    await _create(core_client, "floorplan", allow_single_word=True)
    await _save(core_client, "floorplan", {"views": []})


@pytest.fixture
async def strategy_map(lovelace: None, core_client: HomeAssistantClient) -> None:
    """``map`` as Core's onboarding stores it: a map strategy dashboard."""
    await _create(core_client, "map", allow_single_word=True)
    await _save(core_client, "map", _STRATEGY)


class TestSetDashboardMetadataUpdate:
    """Metadata changes on an existing dashboard reach Core's registry."""

    @pytest.mark.usefixtures("my_dashboard")
    async def test_title_change_on_an_existing_dashboard_is_stored(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await _set_tool(core_client)(url_path="my-dashboard", title="New")

        assert result["success"] is True
        assert result["action"] == "update"
        assert result["metadata_updated"] is True
        assert result["dashboard_created"] is False
        assert (await _row(core_client, "my-dashboard"))["title"] == "New"

    @pytest.mark.usefixtures("my_dashboard")
    async def test_existing_dashboard_with_no_changes_is_rejected(
        self, core_client: HomeAssistantClient
    ) -> None:
        """An existing dashboard cannot report a successful update with no write."""
        with sent_messages(core_client) as spy:
            body = await rejected_call_body(
                _set_tool(core_client)(url_path="my-dashboard")
            )

        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["error"]["message"] == "No dashboard changes were requested"
        assert sent_types(spy) == ["lovelace/dashboards/list"]

    @pytest.mark.usefixtures("my_dashboard")
    async def test_every_metadata_field_is_stored_in_one_update(
        self, core_client: HomeAssistantClient
    ) -> None:
        with sent_messages(core_client) as spy:
            result = await _set_tool(core_client)(
                url_path="my-dashboard",
                title="Updated",
                icon="mdi:home",
                require_admin=True,
                show_in_sidebar=False,
            )

        assert result["metadata_updated"] is True
        assert sent_types(spy).count("lovelace/dashboards/update") == 1
        row = await _row(core_client, "my-dashboard")
        assert row["title"] == "Updated"
        assert row["icon"] == "mdi:home"
        assert row["require_admin"] is True
        assert row["show_in_sidebar"] is False

    @pytest.mark.usefixtures("lovelace")
    async def test_empty_icon_clears_stored_icon(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create(core_client, "my-dashboard", icon="mdi:home")

        result = await _set_tool(core_client)(url_path="my-dashboard", icon="")

        assert result["metadata_updated"] is True
        assert "icon" not in await _row(core_client, "my-dashboard")

    @pytest.mark.usefixtures("my_dashboard")
    async def test_refused_metadata_update_is_reported_with_cores_reason(
        self,
        core_client: HomeAssistantClient,
        read_only_client: HomeAssistantClient,
    ) -> None:
        """Core lets only an admin update a dashboard."""
        body = await rejected_call_body(
            _set_tool(read_only_client)(url_path="my-dashboard", title="Mine")
        )

        assert body["success"] is False
        assert "metadata" in body["error"]["message"].lower()
        assert "Unauthorized" in body["error"]["message"]
        assert (await _row(core_client, "my-dashboard"))["title"] == "Original"

    @pytest.mark.usefixtures("my_dashboard")
    async def test_core_rejection_of_a_metadata_value_is_reported_as_invalid_input(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A bad icon on an existing dashboard is the caller's input, as on create."""
        body = await rejected_call_body(
            _set_tool(core_client)(url_path="my-dashboard", icon="not an icon")
        )

        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "prefix:name" in body["error"]["message"]

    async def test_blank_view_path_rejected_before_write_on_return_screenshot(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A blank view_path with return_screenshot fails before any write."""
        with sent_messages(core_client) as spy:
            body = await rejected_call_body(
                _set_tool(core_client)(
                    url_path="my-dashboard",
                    config={"views": []},
                    return_screenshot=True,
                    view_path="   ",
                )
            )

        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        spy.assert_not_called()

    @pytest.mark.usefixtures("lovelace")
    async def test_metadata_only_update_of_the_unlisted_default_is_rejected(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core does not list the default dashboard on a fresh install, so there
        is no storage id to update: a skipped write is not a success."""
        with sent_messages(core_client) as spy:
            body = await rejected_call_body(
                _set_tool(core_client)(url_path="lovelace", title="My Home")
            )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["error"]["message"] == "No dashboard changes were applied"
        assert "no storage ID" in body["error"]["details"]
        assert sent_types(spy) == ["lovelace/dashboards/list"]

    @pytest.mark.usefixtures("lovelace")
    async def test_config_write_with_unavailable_metadata_uses_warning(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A committed config write reports skipped metadata as a top-level
        warning, and the config lands in Core's default dashboard."""
        replacement = {"views": [{"title": "Home"}]}

        result = await _set_tool(core_client)(
            url_path="lovelace", title="My Home", config=replacement
        )

        assert result["success"] is True
        assert result["config_updated"] is True
        assert result["metadata_updated"] is False
        assert "hint" not in result
        assert (await _stored(core_client, None))["result"] == replacement
        metadata_warning = next(
            warning for warning in result["warnings"] if "no storage ID" in warning
        )
        assert "Dashboard config was saved" in metadata_warning

    @pytest.mark.usefixtures("lovelace")
    async def test_false_booleans_are_not_filtered_out(
        self, core_client: HomeAssistantClient
    ) -> None:
        """False for require_admin/show_in_sidebar is a change, not an omission."""
        await _create(core_client, "my-dashboard", require_admin=True)

        await _set_tool(core_client)(
            url_path="my-dashboard", require_admin=False, show_in_sidebar=False
        )

        row = await _row(core_client, "my-dashboard")
        assert row["require_admin"] is False
        assert row["show_in_sidebar"] is False

    @pytest.mark.usefixtures("my_dashboard")
    async def test_render_paths_follow_the_config_core_holds_after_the_write(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Another edit lands right after the save: the routes reported are
        those of the config Core holds, not of the one submitted."""
        await _save(core_client, "my-dashboard", {"views": []})
        submitted = {"views": [{"title": "Home", "path": "submitted"}]}
        elsewhere = {"views": [{"title": "Home", "path": "edited-elsewhere"}]}
        edits: list[str] = []

        async def edit_elsewhere(event: Event) -> None:
            if event.data["url_path"] == "my-dashboard" and not edits:
                edits.append("done")
                store = hass.data[LOVELACE_DATA].dashboards["my-dashboard"]
                await store.async_save(elsewhere)

        unsubscribe = hass.bus.async_listen(EVENT_LOVELACE_UPDATED, edit_elsewhere)
        try:
            result = await _set_tool(core_client)(
                url_path="my-dashboard", config=submitted
            )
        finally:
            unsubscribe()

        assert result["success"] is True
        assert result["post_write_verified"] is True
        assert result["config_hash"] == compute_config_hash(elsewhere)
        assert result["render_paths"][0]["view_path"] == "edited-elsewhere"
        assert result["render_paths"][0]["render_path"] == (
            "my-dashboard/edited-elsewhere"
        )


class TestSetDashboardListCallDedup:
    """When the pre-resolver fires (internal-id branch), the existence check
    reuses its dashboards list rather than issuing a second
    ``lovelace/dashboards/list`` round-trip; the canonical branch still
    fetches once."""

    @pytest.mark.usefixtures("lovelace")
    async def test_internal_id_branch_calls_list_only_once(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create(core_client, "my-dash")

        with sent_messages(core_client) as spy:
            result = await _set_tool(core_client)(url_path="my_dash", title="Renamed")

        assert sent_types(spy).count("lovelace/dashboards/list") == 1
        assert result["success"] is True
        assert result["resolved_from"] == "my_dash"
        assert (await _row(core_client, "my-dash"))["title"] == "Renamed"

    @pytest.mark.usefixtures("lovelace")
    async def test_canonical_url_path_branch_still_calls_list_once(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create(core_client, "my-dash")

        with sent_messages(core_client) as spy:
            result = await _set_tool(core_client)(url_path="my-dash", title="Renamed")

        assert sent_types(spy).count("lovelace/dashboards/list") == 1
        assert result["success"] is True
        assert "resolved_from" not in result

    @pytest.mark.usefixtures("lovelace")
    async def test_internal_id_no_match_reuses_list_for_create(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A hyphenless identifier matching nothing: the pre-resolver's list
        serves the existence check, and the create goes to Core."""
        await _create(core_client, "other-dash")

        with sent_messages(core_client) as spy:
            await rejected_call_body(
                _set_tool(core_client)(url_path="ghost", title="X")
            )

        assert sent_types(spy) == [
            "lovelace/dashboards/list",
            "lovelace/dashboards/create",
        ]

    async def test_canonical_url_path_branch_rejects_unreadable_registry(
        self, core_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Core without ``lovelace`` refuses the registry read: that is not an
        empty registry, and nothing is created."""
        with (
            sent_messages(core_client) as spy,
            caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        ):
            body = await rejected_call_body(
                _set_tool(core_client)(url_path="my-dash", title="New")
            )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "dashboard registry" in body["error"]["message"]
        assert body["url_path"] == "my-dash"
        assert sent_types(spy) == ["lovelace/dashboards/list"]
        assert any(
            "unexpected shape" in rec.message and "type=dict" in rec.message
            for rec in caplog.records
        ), [rec.message for rec in caplog.records]


class TestSetDashboardUrlPathCreationContract:
    """Core, not HA-MCP, decides whether a new url_path is acceptable."""

    @pytest.mark.usefixtures("floorplan")
    async def test_existing_hyphenless_dashboard_update_is_allowed(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await _set_tool(core_client)(url_path="floorplan", title="Plan")

        assert result["action"] == "update"
        assert result["url_path"] == "floorplan"
        assert result["dashboard_created"] is False
        assert "resolved_from" not in result
        assert (await _row(core_client, "floorplan"))["title"] == "Plan"

    @pytest.mark.usefixtures("lovelace")
    async def test_new_hyphenless_dashboard_relays_core_rejection(
        self, core_client: HomeAssistantClient
    ) -> None:
        body = await rejected_call_body(
            _set_tool(core_client)(url_path="newmap", title="New Map")
        )

        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "Url path needs to contain a hyphen (-)" in body["error"]["message"]
        # Only Core's own wording; the error code's default suggestions remain.
        assert "Try '" not in str(body["error"])

    @pytest.mark.usefixtures("lovelace")
    async def test_core_hyphen_rejection_suggests_hyphenated_path(
        self, core_client: HomeAssistantClient
    ) -> None:
        body = await rejected_call_body(
            _set_tool(core_client)(url_path="my_dash", title="Mine")
        )

        assert body["error"]["suggestion"] == "Try 'my-dash' instead"

    @pytest.mark.usefixtures("lovelace")
    async def test_other_core_create_error_gets_no_hyphen_suggestion(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core refuses the create for its icon: an underscore in the url_path
        does not earn a hyphen suggestion."""
        body = await rejected_call_body(
            _set_tool(core_client)(
                url_path="wall_panel-1", title="Wall", icon="not an icon"
            )
        )

        assert "prefix:name" in body["error"]["message"]
        assert "Try 'wall-panel-1' instead" not in str(body)

    @pytest.mark.usefixtures("lovelace")
    async def test_internal_id_match_rewrites_to_hyphenless_url_path(
        self, core_client: HomeAssistantClient
    ) -> None:
        """``floor-plan`` took the id ``floor_plan``, so Core stores the
        hyphenless ``floor_plan`` under ``floor_plan_2``."""
        await _create(core_client, "floor-plan")
        await _create(core_client, "floor_plan", allow_single_word=True)
        assert (await _row(core_client, "floor_plan"))["id"] == "floor_plan_2"

        result = await _set_tool(core_client)(url_path="floor_plan_2", title="Plan")

        assert result["action"] == "update"
        assert result["url_path"] == "floor_plan"
        assert result["resolved_from"] == "floor_plan_2"
        assert (await _row(core_client, "floor_plan"))["title"] == "Plan"
        assert (await _row(core_client, "floor-plan"))["title"] == "Original"

    async def test_unreadable_registry_is_not_reported_as_bad_hyphenless_input(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core without ``lovelace`` refuses the pre-resolver's registry read."""
        with sent_messages(core_client) as spy:
            body = await rejected_call_body(
                _set_tool(core_client)(url_path="map", title="Updated Map")
            )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "dashboard registry" in body["error"]["message"]
        assert "hyphen" not in body["error"]["message"]
        assert body["url_path"] == "map"
        assert sent_types(spy) == ["lovelace/dashboards/list"]

    @pytest.mark.usefixtures("lovelace")
    async def test_new_hyphenated_dashboard_is_created_with_cores_defaults(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await _set_tool(core_client)(url_path="new-map", title="New Map")

        assert result["success"] is True
        assert result["action"] == "create"
        assert result["dashboard_created"] is True
        assert result["dashboard_id"] == "new_map"
        row = await _row(core_client, "new-map")
        assert row["title"] == "New Map"
        assert row["require_admin"] is False
        assert row["show_in_sidebar"] is True

    @pytest.mark.usefixtures("lovelace")
    async def test_create_without_a_title_gets_cores_refusal_not_an_invented_title(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core requires a title on create; the tool sends only what it was given
        (a blank icon is left out) and relays Core's refusal as invalid input."""
        with sent_messages(core_client) as spy:
            body = await rejected_call_body(
                _set_tool(core_client)(
                    url_path="new-map",
                    icon="",
                    require_admin=True,
                    show_in_sidebar=False,
                )
            )

        creates = [
            call.args[0]
            for call in spy.call_args_list
            if call.args[0]["type"] == "lovelace/dashboards/create"
        ]
        assert creates == [
            {
                "type": "lovelace/dashboards/create",
                "url_path": "new-map",
                "require_admin": True,
                "show_in_sidebar": False,
            }
        ]
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "title" in body["error"]["message"]

    @pytest.mark.usefixtures("floorplan")
    async def test_existing_hyphenless_dashboard_full_config_update_is_allowed(
        self, core_client: HomeAssistantClient
    ) -> None:
        replacement = {"views": [{"title": "Updated"}]}

        result = await _set_tool(core_client)(url_path="floorplan", config=replacement)

        assert result["action"] == "update"
        assert result["config_updated"] is True
        assert "resolved_from" not in result
        assert (await _stored(core_client, "floorplan"))["result"] == replacement

    @pytest.mark.usefixtures("lovelace")
    async def test_create_then_write_config_to_dashboard_without_stored_config(
        self, core_client: HomeAssistantClient
    ) -> None:
        replacement = {"views": [{"title": "Populated"}]}

        create_result = await _set_tool(core_client)(
            url_path="no-config", title="No Config"
        )
        assert create_result["action"] == "create"
        assert any(
            "No config found" in warning
            for warning in create_result.get("warnings", [])
        )

        update_result = await _set_tool(core_client)(
            url_path="no-config", config=replacement
        )

        assert update_result["action"] == "update"
        assert update_result["config_updated"] is True
        assert (await _stored(core_client, "no-config"))["result"] == replacement

    @pytest.mark.usefixtures("lovelace")
    async def test_dashboard_without_stored_config_rejects_supplied_hash(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create(core_client, "no-config")

        body = await rejected_call_body(
            _set_tool(core_client)(
                url_path="no-config",
                config={"views": [{"title": "Populated"}]},
                config_hash="stale-hash",
            )
        )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["error"]["message"] == "Dashboard has no saved config"
        assert body["reason"] == "conflict"
        assert body["write_committed"] is False
        assert body["url_path"] == "no-config"
        assert (await _stored(core_client, "no-config"))["success"] is False

    @pytest.mark.usefixtures("floorplan")
    async def test_existing_hyphenless_dashboard_python_transform_is_allowed(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await _set_tool(core_client)(
            url_path="floorplan",
            python_transform='config["views"].append({"title": "Added"})',
            config_hash=compute_config_hash({"views": []}),
        )

        assert result["action"] == "python_transform"
        assert "resolved_from" not in result
        assert (await _stored(core_client, "floorplan"))["result"] == {
            "views": [{"title": "Added"}]
        }

    @pytest.mark.usefixtures("strategy_map")
    async def test_full_config_cannot_take_control_of_strategy_dashboard(
        self, core_client: HomeAssistantClient
    ) -> None:
        body = await rejected_call_body(
            _set_tool(core_client)(url_path="map", config={"views": []})
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "cannot be converted" in body["error"]["message"]
        assert "Take Control" in body["error"]["suggestion"]
        assert (await _stored(core_client, "map"))["result"] == _STRATEGY

    async def test_full_config_pre_read_failure_blocks_unverified_replacement(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core cannot read the dashboard's current config (its YAML file holds
        no mapping): whether it is a strategy dashboard is unknown, so the
        replacement is not written."""
        await write_config_files(hass, {"broken.yaml": "- not a dashboard\n"})
        assert await async_setup_component(
            hass,
            "lovelace",
            {
                "lovelace": {
                    "dashboards": {
                        "broken-dash": {
                            "mode": "yaml",
                            "filename": "broken.yaml",
                            "title": "Broken",
                        }
                    }
                }
            },
        )

        with sent_messages(core_client) as spy:
            body = await rejected_call_body(
                _set_tool(core_client)(url_path="broken-dash", config={"views": []})
            )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Cannot verify" in body["error"]["message"]
        assert "broken.yaml" in body["error"]["details"]
        assert body["reason"] == "load_failed"
        assert body["write_committed"] is False
        assert body["post_write_verified"] is False
        assert body["url_path"] == "broken-dash"
        assert "lovelace/config/save" not in sent_types(spy)

    @pytest.mark.usefixtures("strategy_map")
    async def test_python_transform_cannot_take_control_of_strategy_dashboard(
        self, core_client: HomeAssistantClient
    ) -> None:
        body = await rejected_call_body(
            _set_tool(core_client)(
                url_path="map",
                python_transform='config = {"views": []}',
                config_hash=compute_config_hash(_STRATEGY),
            )
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "cannot be converted" in body["error"]["message"]
        assert body["action"] == "python_transform"
        assert (await _stored(core_client, "map"))["result"] == _STRATEGY
