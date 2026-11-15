"""What ``ha_get_logs(source="supervisor"|"system_service")`` says when it fails.

Supervisor refuses, answers badly or does not answer, on each route to it
(``_supervisor_logs_core.py``). The tool must name what the caller can do: the
right credential for the route in use, the slug, the permission. The reads that
succeed are in ``test_tools_utility_supervisor_logs.py``.
"""

import logging

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient

from ._core_client import refused_url
from ._supervisor_logs_core import (
    TARGETS,
    Route,
    Target,
    ask,
)
from ._supervisor_logs_core import app_route as app_route
from ._supervisor_logs_core import core_proxy as core_proxy
from ._supervisor_logs_core import core_route as core_route
from ._supervisor_logs_core import route as route
from ._supervisor_logs_core import target as target
from ._tool_errors import rejected_call_body

APP = TARGETS["app"]
SERVICE = TARGETS["service"]


async def refusal(route: Route, target: Target, **overrides: object) -> dict:
    """The body of the error the tool raises for this read."""
    body = await rejected_call_body(ask(route.client, target, **overrides))
    error: dict = body["error"]
    error["context"] = {k: v for k, v in body.items() if k != "error"}
    return error


class TestTheCredentialToFix:
    async def test_a_refused_token_points_at_the_credential_the_route_uses(
        self, route: Route, target: Target
    ):
        route.supervisor.reply("unauthorized", status=401)

        suggestions = (await refusal(route, target))["suggestions"]

        if route.in_app:
            assert any("SUPERVISOR_TOKEN" in s for s in suggestions)
        else:
            # Docker and uvx callers have no Supervisor token to look for.
            assert any("Long-Lived Access Token" in s for s in suggestions)
            assert not any("SUPERVISOR_TOKEN" in s for s in suggestions)

    async def test_a_403_inside_the_app_names_every_authorization_boundary(
        self, app_route: Route, target: Target, caplog: pytest.LogCaptureFixture
    ):
        """Supervisor uses 403 for an unrecognized token, a missing API
        permission and a role too low (#1126)."""
        app_route.supervisor.reply("", status=403)

        with caplog.at_level(logging.WARNING, logger="ha_mcp.client.rest_client"):
            error = await refusal(app_route, target)

        for hint in ("unrecognized", "hassio_api", "hassio_role", "manager"):
            assert hint in error["message"]
        assert any("403" in r.message for r in caplog.records)

    async def test_a_403_inside_the_app_suggests_the_role_for_a_service_log(
        self, app_route: Route
    ):
        app_route.supervisor.reply("", status=403)

        suggestions = (await refusal(app_route, SERVICE))["suggestions"]

        assert any("hassio_role" in s and "manager" in s for s in suggestions)

    async def test_a_403_outside_the_app_asks_for_an_admin_token_not_a_role(
        self, core_route: Route
    ):
        """An admin-only proxy refusing the user is a different fix from the
        app's role: pointing at the role sends a Docker user the wrong way."""
        core_route.supervisor.reply("Unauthorized", status=403)

        suggestions = (await refusal(core_route, SERVICE))["suggestions"]

        assert any("admin" in s.lower() for s in suggestions)
        assert not any("hassio_role" in s for s in suggestions)


class TestWhatSupervisorSays:
    async def test_a_404_carries_supervisors_words_and_the_slug_context(
        self, route: Route, target: Target
    ):
        route.supervisor.reply("Addon is not installed", status=404)

        error = await refusal(route, target)

        assert "Addon is not installed" in error["message"]
        assert error["context"]["slug"] == target.slug
        assert error["context"]["source"] == target.source

    async def test_a_404_for_an_app_suggests_it_is_not_installed(self, route: Route):
        route.supervisor.reply("Addon is not installed", status=404)

        suggestions = (await refusal(route, APP))["suggestions"]

        assert any("not found or not installed" in s for s in suggestions)
        assert any("ha_get_app" in s for s in suggestions)

    async def test_a_400_is_a_service_failure_not_bad_input(self, route: Route):
        """Supervisor rejecting the request is not the caller's validation error."""
        route.supervisor.reply(
            '{"result":"error","message":"Add-on is not running"}', status=400
        )

        error = await refusal(route, APP)

        assert error["code"] == "SERVICE_CALL_FAILED"
        assert any("Supervisor rejected" in s for s in error["suggestions"])
        assert not any("not found or not installed" in s for s in error["suggestions"])
        assert "Add-on is not running" in error["message"]
        assert '{"result"' not in error["message"]

    async def test_an_empty_error_body_falls_back_to_the_reason(
        self, route: Route, target: Target
    ):
        """Not ``API error: 500 - `` with nothing after it (#950)."""
        route.supervisor.reply("", status=500)

        message = (await refusal(route, target))["message"]

        assert "Internal Server Error" in message
        assert not message.endswith(" - ")

    async def test_a_blank_json_message_falls_back_to_the_reason(
        self, core_route: Route
    ):
        core_route.supervisor.reply('{"message": "   "}', status=500)

        message = (await refusal(core_route, APP))["message"]

        assert "Internal Server Error" in message

    async def test_no_body_and_no_reason_still_says_something(
        self, route: Route, target: Target
    ):
        # 599 has no standard reason phrase.
        route.supervisor.reply("", status=599)

        assert "<empty body>" in (await refusal(route, target))["message"]


class TestNoAnswer:
    async def test_a_refused_connection_to_supervisor_names_the_transport(
        self, app_route: Route, monkeypatch: pytest.MonkeyPatch, target: Target
    ):
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())

        error = await refusal(app_route, target)

        assert "Transport error" in error["message"]
        assert any("Check Home Assistant connection" in s for s in error["suggestions"])

    async def test_a_connection_closed_without_an_answer_names_the_transport(
        self, app_route: Route
    ):
        """Not only a refused connect: any transport failure reads the same."""
        app_route.supervisor.drop = True

        error = await refusal(app_route, APP)

        assert "Transport error" in error["message"]

    async def test_an_app_log_read_that_cannot_connect_keeps_the_slug_hint(
        self, app_route: Route, monkeypatch: pytest.MonkeyPatch
    ):
        """A wrong slug can also look like a network failure."""
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())

        suggestions = (await refusal(app_route, APP))["suggestions"]

        assert any(f"Verify app slug '{APP.slug}' is correct" in s for s in suggestions)
        assert any("ha_get_app" in s for s in suggestions)

    async def test_a_core_that_cannot_be_reached_names_the_connection(
        self, core_client: HomeAssistantClient, target: Target
    ):
        unreachable = HomeAssistantClient(
            base_url=refused_url(), token=core_client.token, verify_ssl=False
        )
        try:
            body = await rejected_call_body(ask(unreachable, target))
        finally:
            await unreachable.close()

        assert "Failed to connect" in body["error"]["message"]

    @pytest.mark.timeout(10)
    async def test_a_supervisor_that_never_answers_ends_at_the_deadline(
        self, route: Route
    ):
        """A stalled log body must not wait forever on per-chunk IO (#2279)."""
        route.supervisor.hang = True
        route.client.timeout = 1

        message = (await refusal(route, APP))["message"]

        assert "Timeout" in message
        assert "after 1s" in message
