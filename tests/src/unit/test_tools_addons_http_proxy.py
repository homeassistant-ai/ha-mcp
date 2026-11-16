"""``ha_manage_app``'s HTTP proxy to an app's web API.

The request takes one of three routes to the target app
(``_tools_addons_core.py``): outside the app through Core's ingress view with a
session Supervisor minted, inside the app straight to the app's ingress port,
or, with ``port``, straight to that container port. Each test checks what the
app received or what the tool says when the app cannot be reached, refuses
the request, or answers slowly; the checks the proxy makes before any request
and ``python_transform`` on the answer. A check shared with the WebSocket proxy
runs for both.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.redaction import REDACTED_SET

from ._core_client import free_port
from ._tool_errors import rejected_call_body
from ._tools_addons_core import INGRESS_TOKEN, SLUG, Route, manage
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_route as core_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import fresh_secrets as fresh_secrets
from ._tools_addons_core import quick_app_http as quick_app_http
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

INFO = ("GET", f"/addons/{SLUG}/info")
SESSION = ("POST", "/ingress/session")
FRONT_DOOR = [{"name": "leave_front_door_open", "type": "bool", "optional": True}]


def _front_door(state: object) -> dict[str, Any]:
    """The app's options and schema for one ``leave_front_door_open`` state:
    saved as True or False, in the schema but never saved (a stock install),
    or not an option of the app at all."""
    if state == "unexposed":
        return {"options": {}, "schema": []}
    if state == "absent":
        return {"options": {}, "schema": FRONT_DOOR}
    return {"options": {"leave_front_door_open": state}, "schema": FRONT_DOOR}


def _suggestions(body: dict[str, Any]) -> list[str]:
    error = body["error"]
    return [error.get("suggestion", ""), *error.get("suggestions", [])]


class TestTheRoute:
    async def test_outside_the_app_the_request_goes_through_cores_ingress(
        self, core_route: Route
    ) -> None:
        """With a session Supervisor minted, and without the user's token."""
        core_route.supervisor.running()

        result = await manage(core_route.client, slug=SLUG, path="/api/test")

        assert result["success"] is True
        [seen] = core_route.target.requests
        assert seen.path == "/api/test"
        assert (
            seen.headers["Cookie"]
            == f"ingress_session={core_route.supervisor.sessions[0]}"
        )
        assert "Authorization" not in seen.headers
        assert core_route.supervisor.requests == [INFO, SESSION]

    async def test_inside_the_app_the_request_goes_to_the_apps_ingress_port(
        self, app_route: Route
    ) -> None:
        """The headers the app trusts as Supervisor's ingress, and no session."""
        app_route.supervisor.running()

        await manage(app_route.client, slug=SLUG, path="/api/test")

        [seen] = app_route.target.requests
        assert seen.headers["X-Ingress-Path"] == f"/api/hassio_ingress/{INGRESS_TOKEN}"
        assert seen.headers["X-Hass-Source"] == "core.ingress"
        assert "Cookie" not in seen.headers
        assert "Authorization" not in seen.headers
        assert app_route.supervisor.requests == [INFO]

    async def test_a_direct_port_request_reaches_the_container_without_a_session(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()

        await manage(
            core_route.client, slug=SLUG, path="/flows", port=core_route.target.port
        )

        [seen] = core_route.target.requests
        assert seen.path == "/flows"
        assert "Cookie" not in seen.headers
        assert core_route.supervisor.requests == [INFO]

    async def test_a_session_supervisor_refuses_fails_the_request(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.supervisor.refuse(*SESSION, "Ingress is not available")

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert "Ingress is not available" in body["error"]["message"]
        assert core_route.target.requests == []

    @pytest.mark.parametrize("data", [{}, {"session": ""}], ids=["missing", "empty"])
    async def test_a_session_reply_without_a_token_is_refused(
        self, core_route: Route, data: dict[str, Any]
    ) -> None:
        core_route.supervisor.running()
        core_route.supervisor.answer(*SESSION, data)

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert "ingress session" in body["error"]["message"].lower()
        assert core_route.target.requests == []


class TestBeforeTheRequest:
    @pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
    async def test_a_path_that_climbs_out_of_the_app_is_refused(
        self, core_route: Route, websocket: bool
    ) -> None:
        body = await rejected_call_body(
            manage(
                core_route.client,
                slug=SLUG,
                path="api/%2e%2e/secret",
                websocket=websocket,
            )
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert core_route.supervisor.requests == []

    async def test_dots_inside_a_name_are_not_a_climb(self, core_route: Route) -> None:
        core_route.supervisor.running()

        await manage(core_route.client, slug=SLUG, path="..foo/bar")

        assert [seen.path for seen in core_route.target.requests] == ["/..foo/bar"]

    async def test_an_app_supervisor_does_not_know_fails_the_request(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.refuse(*INFO, f"App {SLUG} does not exist")

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert "does not exist" in body["error"]["message"]
        assert core_route.target.requests == []

    async def test_an_app_without_ingress_needs_a_port(self, core_route: Route) -> None:
        core_route.supervisor.running(ingress=False)

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "does not support Ingress" in body["error"]["message"]

    async def test_a_stopped_app_is_not_called(self, core_route: Route) -> None:
        core_route.supervisor.running(state="stopped")

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "is not running (state: stopped)" in body["error"]["message"]
        assert f"slug='{SLUG}', action='start'" in body["error"]["suggestion"]

    async def test_a_port_does_not_excuse_a_stopped_app(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(ingress=False, state="stopped")

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/ws", websocket=True, port=6052)
        )

        assert "is not running" in body["error"]["message"]

    async def test_an_app_without_an_ingress_entry_is_an_internal_error(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(ingress_entry="")

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "ingress_entry" in body["error"]["message"]

    @pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
    async def test_a_direct_port_needs_the_containers_address(
        self, core_route: Route, websocket: bool
    ) -> None:
        core_route.supervisor.running(ip_address="")

        body = await rejected_call_body(
            manage(
                core_route.client, slug=SLUG, path="/x", port=1880, websocket=websocket
            )
        )

        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "ip_address" in body["error"]["message"]

    @pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
    async def test_inside_the_app_the_ingress_port_is_needed(
        self, app_route: Route, websocket: bool
    ) -> None:
        app_route.supervisor.running(ingress_port=None)

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, path="/x", websocket=websocket)
        )

        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "ingress_port" in body["error"]["message"]


class TestUnreachableApp:
    @pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
    async def test_a_refused_direct_port_suggests_ingress(
        self, core_route: Route, websocket: bool
    ) -> None:
        core_route.supervisor.running()

        body = await rejected_call_body(
            manage(
                core_route.client,
                slug=SLUG,
                path="/x",
                port=free_port(),
                websocket=websocket,
            )
        )

        assert body["error"]["code"] == "CONNECTION_FAILED"
        assert any("route through Ingress" in s for s in _suggestions(body))

    @pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
    async def test_inside_the_app_an_unreachable_app_suggests_restarting_it(
        self, app_route: Route, websocket: bool
    ) -> None:
        """Home Assistant is fine here; the apps' shared network is not."""
        app_route.supervisor.running(ingress_port=free_port())

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, path="/x", websocket=websocket)
        )

        suggestions = _suggestions(body)
        assert any("Docker network may be unhealthy" in s for s in suggestions)
        assert not any(app_route.client.base_url in s for s in suggestions)

    async def test_a_slow_app_times_out_without_blaming_the_route(
        self, core_route: Route, quick_app_http: None
    ) -> None:
        core_route.supervisor.running()
        core_route.target.hang = True

        body = await rejected_call_body(manage(core_route.client, slug=SLUG, path="/x"))

        assert body["error"]["code"] == "TIMEOUT_OPERATION"
        assert "check its status before retrying" in body["error"]["suggestion"]


class TestRefusedRequests:
    async def test_an_ingress_401_points_at_the_session_not_the_app(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.status = 401

        result = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x")
        )

        assert result["status_code"] == 401
        assert "ingress session may have expired" in result["suggestion"]
        assert "addon_config" not in result

    @pytest.mark.parametrize(
        ("status", "front_door", "offered"),
        [
            (401, False, True),
            (401, "unexposed", False),
            (403, "absent", True),
            (403, True, False),
        ],
    )
    async def test_a_direct_port_refusal_offers_the_front_door_only_when_shut(
        self, core_route: Route, status: int, front_door: object, offered: bool
    ) -> None:
        core_route.supervisor.running(
            **_front_door(front_door), ports={"1880/tcp": 1880}
        )
        core_route.target.status = status

        result = await rejected_call_body(
            manage(
                core_route.client, slug=SLUG, path="/flows", port=core_route.target.port
            )
        )

        suggestion = result["suggestion"]
        if offered:
            assert "options={'leave_front_door_open': True}" in suggestion
            assert f"ha_manage_app(slug='{SLUG}', action='restart')" in suggestion
            assert "security trade-off" in suggestion
        else:
            assert "leave_front_door_open" not in suggestion
            assert "app's own authentication and access-control" in suggestion
        assert "ingress session" not in suggestion.lower()

    async def test_an_ingress_403_points_at_the_ip_restriction(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.status = 403

        result = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x")
        )

        assert "Nginx IP restriction" in result["suggestion"]
        assert set(result["addon_config"]) == {
            "options",
            "ports",
            "host_network",
            "ingress_port",
        }

    async def test_an_ingress_403_names_the_lowest_unmapped_port(
        self, core_route: Route
    ) -> None:
        ports = {"9000/tcp": None, "1880/tcp": None, "9999/tcp": 9999}
        core_route.supervisor.running(ports=ports)
        core_route.target.status = 403

        result = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x")
        )

        suggestion = result["suggestion"]
        assert "Map 1880/tcp to a host port" in suggestion
        assert "Configuration → Network" in suggestion
        assert f"ha_manage_app(slug='{SLUG}', path='...', port=1880)" in suggestion
        assert "9999" not in suggestion
        assert "Nginx" not in suggestion

    async def test_an_ingress_403_reads_the_network_map_without_ports(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(network={"8080/tcp": None})
        core_route.target.status = 403

        result = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x")
        )

        assert "port=8080" in result["suggestion"]

    async def test_an_ingress_403_with_a_malformed_port_key_falls_back(
        self, core_route: Route
    ) -> None:
        """A port that is not ``<number>/<proto>`` cannot fill ``port=``."""
        core_route.supervisor.running(ports={"not-a-port": None})
        core_route.target.status = 403

        result = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x")
        )

        assert "Nginx IP restriction" in result["suggestion"]

    async def test_an_ingress_403_shows_only_redacted_options(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(
            options={"github_pat": "github_pat_LIVESECRETVALUE"},
            schema=[{"name": "github_pat", "type": "string", "format": "password"}],
        )
        core_route.target.status = 403

        result = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x")
        )

        assert result["addon_config"]["options"] == {"github_pat": REDACTED_SET}


class TestTransform:
    async def test_a_transform_reshapes_a_json_array(self, core_route: Route) -> None:
        core_route.supervisor.running()
        core_route.target.body = [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]

        result = await manage(
            core_route.client,
            slug=SLUG,
            path="/users",
            python_transform="response = [u['id'] for u in response]",
        )

        assert (result["response"], result["transformed"]) == ([1, 2], True)

    async def test_a_transform_edits_a_json_object_in_place(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.body = {"status": "ok", "details": "..." * 100}

        result = await manage(
            core_route.client,
            slug=SLUG,
            path="/status",
            python_transform="del response['details']",
        )

        assert result["response"] == {"status": "ok"}

    @pytest.mark.parametrize(
        ("expression", "reported"),
        [
            ("open('/etc/passwd')", "Expression validation failed"),
            ("response['missing']['key'] = 1", "Expression raised at runtime"),
        ],
    )
    async def test_a_broken_transform_is_refused(
        self, core_route: Route, expression: str, reported: str
    ) -> None:
        core_route.supervisor.running()

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, path="/x", python_transform=expression)
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert reported in body["error"]["message"]
