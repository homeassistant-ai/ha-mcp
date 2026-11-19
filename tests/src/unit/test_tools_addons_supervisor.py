"""How the app (add-on) tools reach Supervisor, and what they say when it fails.

Inside the app the server asks Supervisor directly with the app's token;
anywhere else Core's ``supervisor/api`` command relays the request
(``_tools_addons_core.py``). Each test asks through the public tools and
checks what Supervisor received and what the tool reports: Supervisor's
refusals by status, failures with no answer, writes whose outcome cannot be
known, the bound on Supervisor's text, and the retry of a busy job group.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import tools_addons

from ._core_client import refused_url
from ._supervisor_logs_core import route as route
from ._tool_errors import rejected_call_body
from ._tools_addons_core import (
    APP_TOKEN,
    SLUG,
    TEXT_LIMIT,
    TRUNCATED,
    Route,
    Supervisor,
    SupervisorWait,
    manage,
    read,
)
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_route as core_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import quick_retry as quick_retry
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import supervisor_wait as supervisor_wait
from ._tools_addons_core import target_app as target_app

INFO = f"/addons/{SLUG}/info"
RESTART = f"/addons/{SLUG}/restart"
STOP = f"/addons/{SLUG}/stop"
COLLISION = f"Another job is running for job group addon_{SLUG}"


class _JumpingClock:
    """The tools' ``time``: an hour passes once Supervisor has seen a request."""

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor

    def monotonic(self) -> float:
        return time.monotonic() + (3600.0 if self.supervisor.requests else 0.0)


@pytest.fixture
def retry_window_elapses(core_route: Route, monkeypatch: pytest.MonkeyPatch) -> None:
    """A busy job group is given up on after its first refusal, not a minute
    later. Only the tools' clock moves, so Core's own timeouts are untouched."""
    monkeypatch.setattr(tools_addons, "time", _JumpingClock(core_route.supervisor))


def _bounded(text: str) -> None:
    assert len(text) <= TEXT_LIMIT
    assert text.endswith(TRUNCATED + " to 50 KiB]")


class TestTheRoute:
    async def test_in_the_app_supervisor_is_asked_directly_with_the_apps_token(
        self, app_route: Route, sent: AsyncMock
    ) -> None:
        app_route.supervisor.running()

        result = await read(app_route.client, slug=SLUG)

        assert result["addon"]["slug"] == SLUG
        headers = app_route.supervisor.headers("GET", INFO)
        assert headers["Authorization"] == f"Bearer {APP_TOKEN}"
        sent.assert_not_awaited()

    async def test_outside_the_app_core_relays_the_request(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()

        result = await read(core_route.client, slug=SLUG)

        assert result["addon"]["slug"] == SLUG
        headers = core_route.supervisor.headers("GET", INFO)
        assert headers["X-Hass-Source"] == "core.websocket_api"


class TestSupervisorRefusals:
    async def test_options_supervisor_rejects_are_a_validation_failure(
        self, route: Route
    ) -> None:
        """#993: "authorized_keys" in Supervisor's message is not an auth error."""
        route.supervisor.running(
            options={"ssh": {"sftp": False}},
            schema=[
                {
                    "name": "ssh",
                    "type": "schema",
                    "optional": False,
                    "multiple": False,
                    "schema": [{"name": "sftp", "type": "boolean", "optional": False}],
                }
            ],
        )
        route.supervisor.refuse(
            "POST",
            f"/addons/{SLUG}/options",
            "Missing option 'authorized_keys' in ssh in SSH (core_ssh)",
        )

        body = await rejected_call_body(
            manage(route.client, slug=SLUG, options={"ssh": {"sftp": True}})
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert route.supervisor.sent("POST", f"/addons/{SLUG}/options") == [
            {"options": {"ssh": {"sftp": True}}}
        ]

    async def test_direct_options_refusal_uses_supervisors_error_key(self, app_route: Route) -> None:
        app_route.supervisor.running(options={}, schema=[])
        app_route.supervisor.reply(
            "POST", f"/addons/{SLUG}/options", status=400,
            payload={
                "result": "error", "error_key": "app_configuration_invalid_error",
                "message": "Configuration declined",
            },
        )
        body = await rejected_call_body(manage(app_route.client, slug=SLUG, options={"debug": True}))
        assert body["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.parametrize(
        ("status", "code", "named"),
        [
            (401, "AUTH_INVALID_TOKEN", "restart the ha-mcp app"),
            (403, "AUTH_INSUFFICIENT_PERMISSIONS", "hassio_role"),
            (400, "SERVICE_CALL_FAILED", "supports apps"),
        ],
    )
    async def test_in_the_app_supervisors_status_decides_the_error(
        self, app_route: Route, status: int, code: str, named: str
    ) -> None:
        app_route.supervisor.refuse("GET", INFO, "Refused by Supervisor", status)

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        assert body["error"]["code"] == code
        assert named in " ".join(body["error"]["suggestions"]).lower()

    async def test_a_403_says_it_may_be_the_token_or_the_role(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.refuse("GET", INFO, "Forbidden", 403)

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        assert body["status_code"] == 403
        assert "token rejection or insufficient API role" in body["error"]["message"]

    async def test_an_endpoint_supervisor_does_not_serve_is_not_found(
        self, route: Route
    ) -> None:
        """Supervisor's router answers an unknown path with a plain-text 404."""
        route.supervisor.reply("GET", INFO, status=404, text="404: Not Found")

        body = await rejected_call_body(read(route.client, slug=SLUG))

        if route.in_app:
            assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
            assert body["error"]["suggestion"] == (
                "Check Home Assistant connection and Supervisor availability"
            )
        else:
            # Core's supervisor/api reports only unknown_error for this endpoint.
            assert body["error"]["code"] == "SERVICE_CALL_FAILED"


class TestNoAnswer:
    async def test_in_the_app_an_unreachable_supervisor_is_a_connection_failure(
        self, app_route: Route, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        assert body["error"]["code"] == "CONNECTION_FAILED"

    async def test_in_the_app_a_read_supervisor_never_answers_times_out(
        self, app_route: Route, supervisor_wait: SupervisorWait
    ) -> None:
        app_route.supervisor.hang("GET", INFO)

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        assert body["error"]["code"] == "CONNECTION_TIMEOUT"

    @pytest.mark.parametrize(
        ("failure", "code"),
        [("refused", "CONNECTION_FAILED"), ("connect-timeout", "CONNECTION_TIMEOUT")],
    )
    async def test_a_write_that_never_left_is_safe_to_retry(
        self,
        app_route: Route,
        supervisor_wait: SupervisorWait,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
        code: str,
    ) -> None:
        if failure == "refused":
            monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())
        else:
            supervisor_wait.connect = 0

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="restart")
        )

        assert body["error"]["code"] == code
        assert "outcome" not in body
        assert app_route.supervisor.requests == []


class TestUnknownOutcome:
    """A write Supervisor may have run must not read as safe to replay."""

    @staticmethod
    def _unknown(body: dict[str, Any], code: str) -> None:
        assert body["error"]["code"] == code
        assert (body["method"], body["outcome"]) == ("POST", "unknown")
        suggestions = " ".join(body["error"]["suggestions"]).lower()
        assert "do not replay" in suggestions
        assert "jobs and logs" in suggestions

    async def test_in_the_app_a_write_supervisor_never_answers(
        self, app_route: Route, supervisor_wait: SupervisorWait
    ) -> None:
        app_route.supervisor.hang("POST", RESTART)

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="restart")
        )

        self._unknown(body, "TIMEOUT_OPERATION")
        assert "ha_get_app" not in " ".join(body["error"]["suggestions"])

    async def test_in_the_app_a_write_whose_connection_drops(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.drop("POST", RESTART)

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="restart")
        )

        self._unknown(body, "CONNECTION_FAILED")

    async def test_outside_the_app_a_write_core_cannot_relay(
        self, core_route: Route
    ) -> None:
        """Core answers a dropped Supervisor request with a blank error."""
        core_route.supervisor.drop("POST", RESTART)

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, action="restart")
        )

        self._unknown(body, "SERVICE_CALL_FAILED")

    @pytest.mark.parametrize(
        ("status", "text"), [(503, "service unavailable"), (307, "")]
    )
    async def test_a_write_answered_by_a_server_error_or_a_redirect(
        self, app_route: Route, status: int, text: str
    ) -> None:
        app_route.supervisor.reply("POST", RESTART, status=status, text=text)

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="restart")
        )

        self._unknown(body, "SERVICE_CALL_FAILED")
        assert body["status_code"] == status
        assert body.get("response_body") == (text or None)

    @pytest.mark.parametrize(
        "answer",
        [
            {"text": "not-json"},
            {"payload": []},
            {"payload": {"result": "unexpected"}},
            {"payload": {"result": "ok", "data": []}},
        ],
        ids=["invalid-json", "non-object", "bad-result", "bad-data"],
    )
    async def test_a_malformed_success_for_a_write(
        self, app_route: Route, answer: dict[str, Any]
    ) -> None:
        app_route.supervisor.reply("POST", RESTART, **answer)

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="restart")
        )

        self._unknown(body, "SERVICE_CALL_FAILED")

    async def test_the_body_of_an_unknown_outcome_is_bounded(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.reply(
            "POST", RESTART, status=503, text="x" * (TEXT_LIMIT + 1)
        )

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="restart")
        )

        _bounded(body["response_body"])


class TestBoundedText:
    """Supervisor's text reaches the model bounded, whatever its shape."""

    async def test_a_huge_refusal_is_bounded(self, route: Route) -> None:
        route.supervisor.refuse("GET", INFO, "y" * (TEXT_LIMIT + 1))

        body = await rejected_call_body(read(route.client, slug=SLUG))

        message = body["error"]["message"]
        assert message.startswith("Command failed: y")
        _bounded(message.removeprefix("Command failed: "))

    async def test_a_huge_plain_text_error_is_bounded(self, app_route: Route) -> None:
        app_route.supervisor.reply("GET", INFO, status=502, text="x" * (TEXT_LIMIT + 1))

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        _bounded(body["error"]["message"].removeprefix("Command failed: "))

    async def test_a_huge_reply_that_is_not_an_object_is_bounded(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.reply("GET", INFO, payload=["x" * 1000] * 200)

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        message = body["error"]["message"]
        assert "Supervisor returned an invalid response" in message
        _bounded(message)

    async def test_a_read_whose_data_is_not_an_object_is_refused_and_bounded(
        self, app_route: Route
    ) -> None:
        """A malformed read never becomes an empty successful app."""
        payload = {"result": "ok", "data": "z" * (TEXT_LIMIT + 1)}
        app_route.supervisor.reply("GET", INFO, payload=payload)

        body = await rejected_call_body(read(app_route.client, slug=SLUG))

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "invalid result payload" in body["error"]["message"]
        assert "outcome" not in body
        _bounded(body["error"]["message"])


class TestBusyJobGroup:
    """Supervisor refuses a call while another job holds the app's job group;
    the refusal comes before the job runs, so the call is retried."""

    async def test_a_collision_is_retried_until_it_clears(
        self, route: Route, quick_retry: None
    ) -> None:
        route.supervisor.refuse("POST", STOP, COLLISION)
        route.supervisor.refuse("POST", STOP, COLLISION)
        route.supervisor.answer("POST", STOP, {})

        result = await manage(route.client, slug=SLUG, action="stop")

        assert result["success"] is True
        assert route.supervisor.requests.count(("POST", STOP)) == 3

    @pytest.mark.parametrize(
        "message",
        [f"App {SLUG} is not installed", "Another job is running"],
        ids=["other-error", "job-level-rejection"],
    )
    async def test_a_refusal_that_is_not_a_group_collision_is_not_retried(
        self, core_route: Route, quick_retry: None, message: str
    ) -> None:
        """The job-level rejection guards long OS operations, not a busy app."""
        core_route.supervisor.refuse("POST", STOP, message)

        await rejected_call_body(manage(core_route.client, slug=SLUG, action="stop"))

        assert core_route.supervisor.requests == [("POST", STOP)]

    async def test_a_real_error_after_a_collision_raises_at_once(
        self, core_route: Route, quick_retry: None
    ) -> None:
        core_route.supervisor.refuse("POST", STOP, COLLISION)
        core_route.supervisor.refuse("POST", STOP, f"App {SLUG} is not installed")
        core_route.supervisor.answer("POST", STOP, {})

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, action="stop")
        )

        assert "not installed" in body["error"]["message"]
        assert core_route.supervisor.requests == [("POST", STOP)] * 2

    async def test_every_attempt_waits_longer_than_supervisor_may_take(
        self, core_route: Route, quick_retry: None, sent: AsyncMock
    ) -> None:
        install = f"/store/addons/{SLUG}/install"
        core_route.supervisor.refuse("POST", install, COLLISION)
        core_route.supervisor.answer("POST", install, {})

        await manage(core_route.client, slug=SLUG, action="install")

        messages = [call.args[0] for call in sent.await_args_list]
        assert len(messages) == 2
        for message in messages:
            assert message["_wait_timeout"] > message["timeout"]

    async def test_a_group_that_never_clears_names_the_stuck_job(
        self, core_route: Route, retry_window_elapses: None
    ) -> None:
        core_route.supervisor.refuse("POST", STOP, COLLISION)

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, action="stop")
        )

        error = body["error"]
        assert COLLISION.lower() in error["message"].lower()
        assert any("Supervisor logs" in s for s in error["suggestions"])
        assert not any("connection" in s.lower() for s in error["suggestions"])

    async def test_the_giving_up_message_is_bounded(
        self, core_route: Route, retry_window_elapses: None
    ) -> None:
        core_route.supervisor.refuse("POST", STOP, COLLISION + "v" * TEXT_LIMIT)

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, action="stop")
        )

        _bounded(body["error"]["message"])
