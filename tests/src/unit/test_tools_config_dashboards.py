"""Dashboard identifier resolution and deletion, against Core's own ``lovelace``.

The dual-accept identifier design rests on three pieces: the unknown-identifier
trigger (``is_unknown_dashboard``), the registry lookup that maps an internal
id or a url_path to both forms, and the lazy resolve + single retry of a
``lovelace/config`` read. They are reached here through the public tools
(``ha_config_get_dashboard``, ``ha_config_delete_dashboard``) on the server's
own client connected to a real ``hass``, so every registry row, config and
error reply is Core's.

Core stores a dashboard under an id slugified from its url_path
(``resolved-path`` becomes ``resolved_path``), so the two forms differ.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import storage
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import tools_config_dashboards
from ha_mcp.tools.dashboard_read_errors import is_unknown_dashboard
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

from ._addon_tool_helpers import _parse_tool_error

_LOGGER_NAME = "ha_mcp.tools.tools_config_dashboards"
_STORED_CONFIG = {"views": [{"title": "Stored", "cards": []}]}


def _sent_types(spy: MagicMock) -> list[str]:
    """The Core command types the spied client sent, in order."""
    return [call.args[0]["type"] for call in spy.call_args_list]


async def _create_dashboard(client: HomeAssistantClient, url_path: str) -> None:
    created = await client.send_websocket_message(
        {"type": "lovelace/dashboards/create", "url_path": url_path, "title": "T"}
    )
    assert created["success"], created


async def _listed_url_paths(client: HomeAssistantClient) -> list[str]:
    listed = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    return [row["url_path"] for row in listed["result"]]


@pytest.fixture
async def lovelace(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
async def internal_id_dashboard(
    hass: HomeAssistant, lovelace: None, core_client: HomeAssistantClient
) -> None:
    """``resolved-path``, which Core stores under the id ``resolved_path``."""
    await _create_dashboard(core_client, "resolved-path")
    saved = await core_client.send_websocket_message(
        {
            "type": "lovelace/config/save",
            "url_path": "resolved-path",
            "config": _STORED_CONFIG,
        }
    )
    assert saved["success"], saved


# -----------------------------------------------------------------------------
# is_unknown_dashboard — Core's own messages
# -----------------------------------------------------------------------------


class TestIsUnknownDashboard:
    """The message is the only signal that tells Core's two ``config_not_found``
    causes apart at the tool layer. Pin it against Core's real wording so an
    HA-side change is caught here rather than degrading to "lazy fallback
    never fires" silently."""

    @pytest.mark.usefixtures("lovelace")
    async def test_cores_unknown_identifier_reply_is_recognised(
        self, core_client: HomeAssistantClient
    ) -> None:
        reply = await core_client.send_websocket_message(
            {"type": "lovelace/config", "url_path": "never-created"}
        )

        assert reply["success"] is False
        assert is_unknown_dashboard(str(reply["error"])) is True

    @pytest.mark.usefixtures("lovelace")
    async def test_cores_empty_dashboard_reply_is_not_an_unknown_identifier(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core's "No config found." for a dashboard with nothing saved must NOT
        trigger a lazy retry, otherwise the caller's empty-state path is hidden."""
        await _create_dashboard(core_client, "empty-dash")
        reply = await core_client.send_websocket_message(
            {"type": "lovelace/config", "url_path": "empty-dash"}
        )

        assert reply["success"] is False
        assert is_unknown_dashboard(str(reply["error"])) is False


# -----------------------------------------------------------------------------
# Lazy resolve + single retry of the get-mode read
# -----------------------------------------------------------------------------


class TestLazyResolveAndRetry:
    @pytest.mark.usefixtures("internal_id_dashboard")
    async def test_known_url_path_reads_without_a_registry_lookup(
        self, core_client: HomeAssistantClient
    ) -> None:
        with patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as spy:
            result = await DashboardConfigTools(core_client).ha_config_get_dashboard(
                url_path="resolved-path"
            )

        assert result["config"] == _STORED_CONFIG
        assert "resolved_from" not in result
        assert "lovelace/dashboards/list" not in _sent_types(spy)

    @pytest.mark.usefixtures("lovelace")
    async def test_empty_dashboard_reply_does_not_resolve(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A failure other than the unknown-identifier one must NOT invoke the
        resolver: the caller sees Core's own answer."""
        await _create_dashboard(core_client, "empty-dash")
        with patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as spy:
            result = await DashboardConfigTools(core_client).ha_config_get_dashboard(
                url_path="empty-dash"
            )

        assert result["stored_config"] is False
        assert "lovelace/dashboards/list" not in _sent_types(spy)

    @pytest.mark.usefixtures("lovelace")
    async def test_unmatched_identifier_reports_cores_error(
        self, core_client: HomeAssistantClient
    ) -> None:
        """The resolver finds nothing: Core's original reply wins, as a 404."""
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                wraps=core_client.send_websocket_message,
            ) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await DashboardConfigTools(core_client).ha_config_get_dashboard(
                url_path="ghost-dash"
            )

        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert is_unknown_dashboard(error["error"]["message"])
        assert _sent_types(spy).count("lovelace/dashboards/list") == 1
        assert _sent_types(spy).count("lovelace/config") == 1

    @pytest.mark.usefixtures("internal_id_dashboard")
    async def test_resolver_without_answer_falls_through_to_cores_error(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The registry read gets no answer: it must NOT escape; it is logged
        and the caller's error path surfaces Core's real reply."""
        monkeypatch.setattr(
            tools_config_dashboards,
            "fetch_dashboards_list",
            AsyncMock(side_effect=HomeAssistantConnectionError("ws gone")),
        )

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
            pytest.raises(ToolError) as exc_info,
        ):
            await DashboardConfigTools(core_client).ha_config_get_dashboard(
                url_path="resolved_path"
            )

        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert is_unknown_dashboard(error["error"]["message"])
        assert any("Lazy resolver failed" in rec.message for rec in caplog.records)

    @pytest.mark.usefixtures("internal_id_dashboard")
    async def test_internal_id_resolves_and_retries_with_the_url_path(
        self, core_client: HomeAssistantClient
    ) -> None:
        with patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as spy:
            result = await DashboardConfigTools(core_client).ha_config_get_dashboard(
                url_path="resolved_path"
            )

        assert result["config"] == _STORED_CONFIG
        assert result["url_path"] == "resolved-path"
        assert result["resolved_from"] == "resolved_path"
        reads = [
            call.args[0]
            for call in spy.call_args_list
            if call.args[0]["type"] == "lovelace/config"
        ]
        # The first read keeps the caller's identifier (its message is not
        # rewritten in place); the single retry carries the canonical url_path.
        assert [read["url_path"] for read in reads] == [
            "resolved_path",
            "resolved-path",
        ]


# -----------------------------------------------------------------------------
# ha_config_delete_dashboard — registry lookup and result shapes (#1300)
# -----------------------------------------------------------------------------


class TestDeleteDashboard:
    """Pin the 404 and idempotent-success shapes on ``ha_config_delete_dashboard``
    (issue #1300): ``RESOURCE_NOT_FOUND`` with top-level ``action`` + ``url_path``,
    no ``resource_type`` / ``identifier``, and the success path's matching key
    set on the already-deleted idempotent branch.
    """

    @pytest.mark.usefixtures("internal_id_dashboard")
    async def test_internal_id_deletes_the_dashboard(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await DashboardConfigTools(core_client).ha_config_delete_dashboard(
            url_path="resolved_path"
        )

        assert result["success"] is True
        assert "resolved-path" not in await _listed_url_paths(core_client)

    @pytest.mark.usefixtures("internal_id_dashboard")
    async def test_url_path_deletes_by_the_registry_id(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await DashboardConfigTools(core_client).ha_config_delete_dashboard(
            url_path="resolved-path"
        )

        assert result["success"] is True
        assert result["resolved_id"] == "resolved_path"
        assert "resolved-path" not in await _listed_url_paths(core_client)

    @pytest.mark.usefixtures("lovelace")
    async def test_unresolvable_url_path_raises_resource_not_found(
        self, core_client: HomeAssistantClient
    ) -> None:
        """No registry match: the canonical RESOURCE_NOT_FOUND shape with
        top-level ``action`` and ``url_path`` matching the idempotent
        already-deleted success branch, naming the dashboards that exist."""
        await _create_dashboard(core_client, "real-dash")

        with pytest.raises(ToolError) as exc_info:
            await DashboardConfigTools(core_client).ha_config_delete_dashboard(
                url_path="ghost-dash"
            )

        body = _parse_tool_error(exc_info)
        assert body["success"] is False
        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "ghost-dash" in body["error"]["message"]
        assert "real-dash" in json.dumps(body)
        assert body["action"] == "delete"
        assert body["url_path"] == "ghost-dash"
        # The pre-#1300 hard-coded resource_type / identifier are dropped —
        # they were the source of the cross-family shape divergence.
        assert "resource_type" not in body
        assert "identifier" not in body

    async def test_unreadable_registry_raises_service_failure(
        self, core_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A registry reply without a dashboard list (Core without ``lovelace``
        refuses the command) is logged and is not reported as a missing
        dashboard, and nothing is deleted."""
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                wraps=core_client.send_websocket_message,
            ) as spy,
            caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
            pytest.raises(ToolError) as exc_info,
        ):
            await DashboardConfigTools(core_client).ha_config_delete_dashboard(
                url_path="existing-dash"
            )

        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "dashboard registry" in body["error"]["message"]
        assert "not found" not in body["error"]["message"].lower()
        assert body["action"] == "delete"
        assert body["url_path"] == "existing-dash"
        assert any("unexpected shape" in rec.message for rec in caplog.records)
        assert "lovelace/dashboards/delete" not in _sent_types(spy)

    @pytest.mark.usefixtures("lovelace")
    async def test_already_deleted_returns_idempotent_success_shape(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another client deletes the dashboard between the registry read and
        the delete: Core's ``not_found`` reply is the idempotent success shape,
        with the ``action`` + ``url_path`` keys the 404 path mirrors."""
        await _create_dashboard(core_client, "stale-dash")
        real_fetch = tools_config_dashboards.fetch_dashboards_list

        async def fetch_then_deleted_elsewhere(
            client: HomeAssistantClient,
        ) -> list[dict[str, Any]] | None:
            rows = await real_fetch(client)
            gone = await client.send_websocket_message(
                {"type": "lovelace/dashboards/delete", "dashboard_id": "stale_dash"}
            )
            assert gone["success"], gone
            return rows

        monkeypatch.setattr(
            tools_config_dashboards,
            "fetch_dashboards_list",
            fetch_then_deleted_elsewhere,
        )

        result = await DashboardConfigTools(core_client).ha_config_delete_dashboard(
            url_path="stale-dash"
        )

        assert result["success"] is True
        assert result["action"] == "delete"
        assert result["url_path"] == "stale-dash"
        assert "resource_type" not in result
        assert "identifier" not in result

    @pytest.mark.usefixtures("lovelace")
    async def test_not_found_text_without_core_code_is_a_failure(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only Core's ``not_found`` code means already deleted, not message text."""
        await _create_dashboard(core_client, "stale-dash")
        # Core fails to remove the dashboard's storage file.
        monkeypatch.setattr(
            storage.Store,
            "async_remove",
            AsyncMock(side_effect=HomeAssistantError("Storage file not found")),
        )

        with pytest.raises(ToolError) as exc_info:
            await DashboardConfigTools(core_client).ha_config_delete_dashboard(
                url_path="stale-dash"
            )

        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Storage file not found" in body["error"]["message"]

    async def test_yaml_mode_dashboard_gets_a_precise_error(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A YAML row has no collection id; the error names YAML mode and
        nothing is deleted."""
        assert await async_setup_component(
            hass,
            "lovelace",
            {
                "lovelace": {
                    "dashboards": {
                        "yaml-dash": {
                            "mode": "yaml",
                            "filename": "yaml-dash.yaml",
                            "title": "Y",
                        }
                    }
                }
            },
        )
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                wraps=core_client.send_websocket_message,
            ) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await DashboardConfigTools(core_client).ha_config_delete_dashboard(
                url_path="yaml-dash"
            )

        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "YAML-mode" in body["error"]["message"]
        assert "configuration.yaml" in body["error"]["details"]
        assert "lovelace/dashboards/delete" not in _sent_types(spy)

    async def test_default_alias_deletes_the_lovelace_item(
        self,
        hass: HomeAssistant,
        hass_storage: dict[str, Any],
        core_client: HomeAssistantClient,
    ) -> None:
        """'default' maps to the migrated ``lovelace`` item, which Core deletes."""
        # Core migrates a stored default config to its ``lovelace`` dashboard item.
        hass_storage["lovelace"] = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace",
            "data": {"config": _STORED_CONFIG},
        }
        assert await async_setup_component(hass, "lovelace", {})
        assert "lovelace" in await _listed_url_paths(core_client)

        result = await DashboardConfigTools(core_client).ha_config_delete_dashboard(
            url_path="default"
        )

        assert result["success"] is True
        assert result["url_path"] == "lovelace"
        assert "auto-generated default dashboard" in result["note"]
        assert "lovelace" not in await _listed_url_paths(core_client)
