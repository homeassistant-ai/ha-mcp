"""``ha_config_get_dashboard`` error handling and search disclosure warnings.

Covers:
 - Search mode: when Home Assistant gives no answer, the structured error
   carries a classified code and dashboard suggestions, never internal Python
   type names or tracebacks.
 - List mode (list_only=True): a reply without a dashboard list emits a
   warning and returns an empty list, not a silent degradation.
 - Search disclosure warnings at the tool boundary.
 - The error context of an unexpected failure after lazy-resolve names the
   resolved url_path.

Every reply is Core's own: the tool runs on the server's client connected to a
real ``hass`` with ``lovelace`` set up.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import tools_config_dashboards
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

from ._dashboard_search_helpers import SEARCH_URL_PATH, store_dashboard
from ._tool_errors import error_body


async def _search_without_answer(
    hass: HomeAssistant, client: HomeAssistantClient, kind: str = "dropped"
) -> dict[str, Any]:
    """Search a stored dashboard while Home Assistant gives no answer."""
    await store_dashboard(hass, client, {"views": [{"cards": []}]})
    if kind == "dropped":
        no_answer = patch.object(
            client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("Connection lost")),
        )
    else:
        # The socket stops answering; the client turns that into its own
        # no-answer error, as it does for a real command timeout.
        no_answer = patch.object(
            HomeAssistantWebSocketClient,
            "send_command",
            AsyncMock(side_effect=HomeAssistantCommandTimeout("Command timeout")),
        )
    with no_answer, pytest.raises(ToolError) as exc_info:
        await DashboardConfigTools(client).ha_config_get_dashboard(
            url_path=SEARCH_URL_PATH, entity_id="light.test"
        )
    return error_body(exc_info)


class TestConfigGetDashboardSearchErrorHandling:
    """Search mode's error path does not leak internals."""

    async def test_error_does_not_leak_internals(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Error response must NOT contain 'error_type' or 'traceback'."""
        result = await _search_without_answer(hass, core_client)

        assert result["success"] is False
        assert isinstance(result["error"], dict), (
            "error must be structured dict, not raw string"
        )
        assert "code" in result["error"]
        assert "message" in result["error"]
        assert "error_type" not in result
        assert "traceback" not in result

    async def test_error_includes_suggestions(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Error response must include dashboard-specific suggestions."""
        result = await _search_without_answer(hass, core_client)

        suggestions = result["error"]["suggestions"]
        assert "Check HA connection" in suggestions
        assert (
            "Verify dashboard with ha_config_get_dashboard(list_only=True)"
            in suggestions
        )

    @pytest.mark.parametrize(
        ("kind", "expected_code"),
        [("dropped", "CONNECTION_FAILED"), ("timeout", "CONNECTION_TIMEOUT")],
    )
    async def test_no_answer_kinds_produce_their_error_codes(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        kind: str,
        expected_code: str,
    ) -> None:
        """A dropped connection and a timeout are told apart by error code."""
        result = await _search_without_answer(hass, core_client, kind)

        assert result["success"] is False
        assert result["error"]["code"] == expected_code


class TestGetDashboardListOnlyUnexpectedShape:
    """list_only=True warns (not fails) when the reply carries no dashboard list.

    fetch_dashboards_list logs at WARNING and returns None; the ``or []``
    fallback means the tool still returns a valid success response with an
    empty dashboards list. Core without ``lovelace`` answers the list command
    with an error reply, which has no list in it.
    """

    async def test_reply_without_list_logs_warning_and_returns_empty_list(
        self,
        core_client: HomeAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
        ):
            result = await DashboardConfigTools(core_client).ha_config_get_dashboard(
                list_only=True
            )

        assert result["success"] is True
        assert result["dashboards"] == []
        assert result["count"] == 0
        assert any(
            "unexpected shape" in rec.message and "type=dict" in rec.message
            for rec in caplog.records
        ), (
            f"expected an 'unexpected shape' warning naming the response "
            f"type; got {[rec.message for rec in caplog.records]}"
        )


class TestFindCardDisclosureWarnings:
    """Response-level ``warnings[]`` for the find-card disclosure layer.

    Pins the issue #1599 round-2 item-1 behaviour at the tool boundary: the
    warning keys off the *presence* of a non-traversed child-bearing shape
    (collected during the walk), not off a 0-match — so a matching un-walkable
    container no longer suppresses it and a true negative no longer cries wolf.
    """

    async def _search(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        config: dict[str, Any],
        **criteria: Any,
    ) -> dict[str, Any]:
        await store_dashboard(hass, client, config)
        result = await DashboardConfigTools(client).ha_config_get_dashboard(
            url_path=SEARCH_URL_PATH, **criteria
        )
        assert isinstance(result, dict)
        return result

    async def test_warns_on_uncoverable_shape_even_when_matched(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A picture-elements card that itself matches still triggers the warning
        — the suppressible-warning bug is closed. Warnings are a top-level
        ``list[str]`` (return contract)."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "picture-elements",
                            "image": "/local/x.png",
                            "elements": [{"type": "state-badge", "entity": "light.pe"}],
                        }
                    ]
                }
            ]
        }
        result = await self._search(
            hass, core_client, config, card_type="picture-elements"
        )
        assert result["match_count"] == 1  # the container matched
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        assert all(isinstance(w, str) for w in warnings)
        assert any(".views[0].cards[0].elements" in w for w in warnings), warnings

    async def test_silent_on_true_negative_without_shape(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A 0-match over a fully-coverable dashboard emits no disclosure warning
        — the cry-wolf bug is closed."""
        config = {"views": [{"cards": [{"type": "tile", "entity": "light.plain"}]}]}
        result = await self._search(hass, core_client, config, card_type="nonexistent")
        assert result["match_count"] == 0
        assert "warnings" not in result


class TestResolvedUrlPathInErrorContext:
    """An unexpected exception raised after lazy-resolve must report the
    resolved url_path in error context, not the caller's original
    (pre-resolve) identifier.

    Regression test for the C901 decomposition of ha_config_get_dashboard:
    splitting the mode dispatch into separate mode helpers moved the url_path
    reassignment into a helper's local scope, so the outer method's
    except-block context silently fell back to reporting the unresolved
    identifier.

    Core stores a dashboard under an id slugified from its url_path
    (``resolved-path`` becomes ``resolved_path``); the tool is given that id.
    """

    @pytest.fixture
    async def internal_id_dashboard(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "lovelace", {})
        created = await core_client.send_websocket_message(
            {
                "type": "lovelace/dashboards/create",
                "url_path": "resolved-path",
                "title": "Resolved",
            }
        )
        assert created["success"], created
        saved = await core_client.send_websocket_message(
            {
                "type": "lovelace/config/save",
                "url_path": "resolved-path",
                "config": {"views": [{"cards": []}]},
            }
        )
        assert saved["success"], saved

    @pytest.mark.usefixtures("internal_id_dashboard")
    @pytest.mark.parametrize(
        "criteria", [{"card_type": "tile"}, {}], ids=["search", "get"]
    )
    async def test_failure_after_resolve_reports_resolved_url_path(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        criteria: dict[str, Any],
    ) -> None:
        tool = DashboardConfigTools(core_client).ha_config_get_dashboard
        resolved = await tool(url_path="resolved_path", **criteria)
        assert resolved["url_path"] == "resolved-path"
        # An unexpected failure in our own code once the config is read.
        monkeypatch.setattr(
            tools_config_dashboards,
            "compute_config_hash",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tool(url_path="resolved_path", **criteria)

        assert error_body(exc_info)["url_path"] == "resolved-path"
