"""The Restart button inside the app: ``POST /api/settings/restart``.

The app restarts itself through Supervisor after the answer has gone out, so
that ingress does not turn the dropped connection into a "Restart failed"; a
named sibling app is restarted at once and Supervisor's answer reported. The
handlers ask the stand-in Supervisor of ``_tools_addons_core`` directly, as
the app does; Core's clock lets the delayed self-restart run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient

from ._core_client import refused_url
from ._settings_ui_app import (
    APP_TOKEN,
    SELF_RESTART,
    app_server,
    ha_mcp_tasks,
    in_app,
    settings_http,
)
from ._settings_ui_app import settings_world as settings_world
from ._tools_addons_core import Supervisor, outwait, timers
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

RESTART = "/api/settings/restart"
SIBLING = "core_ssh"
SIBLING_RESTART = ("POST", f"/addons/{SIBLING}/restart")


async def _restart(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient, body: Any = None
) -> httpx.Response:
    async with settings_http(app_server(monkeypatch, client)) as http:
        if body is None:
            return await http.post(RESTART)
        return await http.post(RESTART, json=body)


async def _self_restart(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    client: HomeAssistantClient,
    body: Any = None,
) -> httpx.Response:
    """Press Restart, then let the delayed self-restart run to its end."""
    armed_before, tasks_before = timers(hass), ha_mcp_tasks()
    response = await _restart(monkeypatch, client, body)
    [restart] = ha_mcp_tasks() - tasks_before
    await outwait(hass, 0, 1, armed_before)
    await asyncio.gather(restart)
    return response


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What ha-mcp logged at ERROR (the stand-in Supervisor logs its own drops)."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.name.startswith("ha_mcp")
    ]


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor) -> Supervisor:
    in_app(monkeypatch, supervisor)
    return supervisor


async def test_outside_the_app_restart_is_refused(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    """Nothing there to ask Supervisor with."""
    response = await _restart(monkeypatch, core_client)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"


class TestSelfRestart:
    async def test_the_answer_goes_out_before_supervisor_is_asked(
        self,
        hass: HomeAssistant,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """Otherwise ingress answers the page with a 5xx for a restart that worked."""
        armed_before, tasks_before = timers(hass), ha_mcp_tasks()

        response = await _restart(monkeypatch, core_client)

        assert response.status_code == 200
        assert "Restart initiated" in response.json()["message"]
        assert app.requests == []
        [restart] = ha_mcp_tasks() - tasks_before
        await outwait(hass, 0, 1, armed_before)
        await asyncio.gather(restart)
        assert app.requests == [SELF_RESTART]
        assert app.headers(*SELF_RESTART)["Authorization"] == f"Bearer {APP_TOKEN}"

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"slug": "   "},
            {"slug": 42},
            "not-a-dict",
            {"slug": "../evil"},
        ],
        ids=["no-slug", "blank", "not-a-string", "not-an-object", "path-traversal"],
    )
    async def test_a_body_without_a_usable_slug_restarts_the_app_itself(
        self,
        hass: HomeAssistant,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        body: Any,
    ) -> None:
        """The page posts no body; a slug that is not a plain name must not build
        some other Supervisor path."""
        response = await _self_restart(hass, monkeypatch, core_client, body)

        assert response.status_code == 200
        assert app.requests == [SELF_RESTART]

    async def test_supervisor_ending_the_app_mid_request_logs_no_error(
        self,
        hass: HomeAssistant,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Supervisor stopping the app is how the restart goes."""
        app.drop(*SELF_RESTART)

        with caplog.at_level(logging.ERROR, logger="ha_mcp"):
            await _self_restart(hass, monkeypatch, core_client)

        assert app.requests == [SELF_RESTART]
        assert _errors(caplog) == []

    async def test_a_token_gone_before_the_restart_runs_is_logged(
        self,
        hass: HomeAssistant,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The page was already told the restart began; the log says why not."""
        armed_before, tasks_before = timers(hass), ha_mcp_tasks()
        await _restart(monkeypatch, core_client)
        [restart] = ha_mcp_tasks() - tasks_before
        monkeypatch.delenv("SUPERVISOR_TOKEN")

        with caplog.at_level(logging.ERROR, logger="ha_mcp"):
            await outwait(hass, 0, 1, armed_before)
            await asyncio.gather(restart)

        assert app.requests == []
        assert any("SUPERVISOR_TOKEN unset" in message for message in _errors(caplog))


class TestSiblingRestart:
    async def test_a_named_app_is_restarted_at_once(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """The in-app E2E suite restarts another app instead of itself."""
        app.answer(*SIBLING_RESTART)

        response = await _restart(monkeypatch, core_client, {"slug": SIBLING})

        assert response.status_code == 200
        assert app.requests == [SIBLING_RESTART]

    async def test_supervisor_dropping_the_request_counts_as_restarted(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """Supervisor stops the target mid-request."""
        app.drop(*SIBLING_RESTART)

        response = await _restart(monkeypatch, core_client, {"slug": SIBLING})

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert "Restart initiated" in response.json()["message"]

    async def test_a_refused_restart_is_reported(
        self,
        app: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        app.refuse(*SIBLING_RESTART, "Unauthorized", status=401)

        response = await _restart(monkeypatch, core_client, {"slug": SIBLING})

        assert response.status_code == 502
        assert response.json()["success"] is False

    async def test_an_unreachable_supervisor_is_not_a_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        socket_enabled: None,
    ) -> None:
        """No connection was made, so nothing was restarted."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())

        response = await _restart(monkeypatch, core_client, {"slug": SIBLING})

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "CONNECTION_FAILED"
