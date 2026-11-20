"""``ha_manage_app``'s WebSocket proxy to an app's WebSocket API.

The target app (``_tools_addons_core.py``) sends frames and closes, stays open,
or refuses the handshake. Each test checks the route the connection took and
what the tool returns: the frames parsed and cleaned, why collection stopped,
the window ``message_offset`` / ``message_limit`` select, the elision of long
config dumps, ``python_transform``, and the error for a refused, slow or
dropped connection. Waits are passed by moving Core's clock, not sat through.
A call that stops collecting early runs inside the app, and leaves fewer frames
unread than the client buffers: Core's ingress proxy never answers a close the
client starts, and a full buffer stops the client reading the end of the
connection, either of which makes the call sit out its close timeout.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant.core import HomeAssistant

from ._tool_errors import rejected_call_body
from ._tools_addons_core import (
    INGRESS_TOKEN,
    SLUG,
    TEXT_LIMIT,
    Route,
    armed,
    manage,
    outwait,
    timers,
)
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_route as core_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

YAML = [f"  key_{i}: value" for i in range(30)]


async def _collect(route: Route, *frames: str | bytes, **overrides: object) -> dict:
    """The tool's result for an app that sends ``frames`` and closes."""
    route.supervisor.running()
    route.target.frames = list(frames)
    return await manage(
        route.client, slug=SLUG, path="/ws", websocket=True, **overrides
    )


class TestTheRoute:
    async def test_outside_the_app_it_goes_through_cores_ingress(
        self, core_route: Route
    ) -> None:
        """With a session Supervisor minted, and without the user's token."""
        result = await _collect(core_route, "hello")

        assert result["messages"] == ["hello"]
        [seen] = core_route.target.requests
        assert seen.path == "/ws"
        assert (
            seen.headers["Cookie"]
            == f"ingress_session={core_route.supervisor.sessions[0]}"
        )
        assert "Authorization" not in seen.headers

    async def test_inside_the_app_it_goes_to_the_apps_ingress_port(
        self, app_route: Route
    ) -> None:
        await _collect(app_route, "hello")

        [seen] = app_route.target.requests
        assert seen.headers["X-Ingress-Path"] == f"/api/hassio_ingress/{INGRESS_TOKEN}"
        assert seen.headers["X-Hass-Source"] == "core.ingress"
        assert "Cookie" not in seen.headers
        assert ("POST", "/ingress/session") not in app_route.supervisor.requests

    async def test_a_direct_port_reaches_an_app_without_ingress_or_a_session(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(ingress=False)

        result = await manage(
            core_route.client,
            slug=SLUG,
            path="/compile",
            websocket=True,
            port=core_route.target.port,
        )

        assert result["closed_by"] == "server_closed"
        [seen] = core_route.target.requests
        assert seen.path == "/compile"
        assert "Cookie" not in seen.headers
        assert ("POST", "/ingress/session") not in core_route.supervisor.requests


class TestFrames:
    async def test_json_frames_are_parsed_and_text_kept(
        self, core_route: Route
    ) -> None:
        result = await _collect(
            core_route,
            '{"event": "line", "data": "Compiling..."}',
            "plain text",
            '{"event": "exit", "code": 0}',
        )

        assert result["messages"] == [
            {"event": "line", "data": "Compiling..."},
            "plain text",
            {"event": "exit", "code": 0},
        ]
        assert (result["message_count"], result["closed_by"]) == (3, "server_closed")
        assert "pagination" not in result

    async def test_terminal_colour_codes_are_stripped(self, core_route: Route) -> None:
        result = await _collect(core_route, "\x1b[32mSUCCESS\x1b[0m Build complete")

        assert result["messages"] == ["SUCCESS Build complete"]

    async def test_binary_frames_are_dropped(self, core_route: Route) -> None:
        result = await _collect(core_route, b"\x00\x01\x02", "text message")

        assert result["messages"] == ["text message"]


class TestWhyCollectionStopped:
    async def test_without_wait_for_close_it_returns_after_silence(
        self, hass: HomeAssistant, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.frames = ["first response"]
        core_route.target.hold = asyncio.Event()
        core_route.target.stay_open = True
        before = timers(hass)
        call = asyncio.create_task(
            manage(
                core_route.client,
                slug=SLUG,
                path="/ws",
                websocket=True,
                wait_for_close=False,
            )
        )

        # The frame comes once the call waits for it; the silence after it ends the call.
        await armed(hass, 1, 2.5, before)
        waiting = timers(hass)
        core_route.target.hold.set()
        await outwait(hass, 1, 2.5, waiting)

        result = await call
        assert (result["messages"], result["closed_by"]) == (
            ["first response"],
            "silence",
        )

    async def test_a_stream_that_never_closes_reports_a_timeout(
        self, hass: HomeAssistant, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.stay_open = True
        before = timers(hass)
        call = asyncio.create_task(
            manage(core_route.client, slug=SLUG, path="/ws", websocket=True)
        )

        await outwait(hass, 100, 121, before)

        assert (await call)["closed_by"] == "timeout"

    async def test_message_limit_stops_collection(self, app_route: Route) -> None:
        frames = [f"msg {i}" for i in range(10)]

        result = await _collect(app_route, *frames, message_limit=5, summarize=False)

        assert result["messages"] == frames[:5]
        assert result["closed_by"] == "message_limit"
        assert result["pagination"]["total_collected"] == 5

    async def test_the_safety_ceiling_is_told_apart_from_message_limit(
        self, app_route: Route
    ) -> None:
        frames = [f"msg {i}" for i in range(1001)]

        result = await _collect(app_route, *frames, summarize=False)

        assert result["closed_by"] == "safety_ceiling"
        assert result["message_count"] < len(frames)

    async def test_collection_stops_at_the_size_limit(self, app_route: Route) -> None:
        frames = ["x" * 1024] * (TEXT_LIMIT // 1024 + 1)

        result = await _collect(app_route, *frames, summarize=False)

        assert result["closed_by"] == "size_limit"
        assert result["message_count"] < len(frames)

    async def test_a_result_too_large_to_return_is_replaced_by_a_hint(
        self, core_route: Route
    ) -> None:
        """Escaping doubles a frame of quotes once it is serialised."""
        result = await _collect(core_route, '"' * 30_000, summarize=False)

        assert result["error"] == "RESPONSE_TOO_LARGE"
        assert (result["truncated"], result["message_count"]) == (True, 1)
        assert "message_limit" in result["hint"]
        assert "messages" not in result


class TestTheWindow:
    @pytest.mark.parametrize(
        ("offset", "limit", "returned"),
        [
            (2, None, ["m2", "m3", "m4"]),
            (1, 2, ["m1", "m2"]),
            (10, None, []),
            (-5, None, ["m0", "m1", "m2", "m3", "m4"]),
            (0, -1, []),
        ],
        ids=[
            "offset",
            "offset-and-limit",
            "past-the-end",
            "negative-offset",
            "negative-limit",
        ],
    )
    async def test_offset_and_limit_select_the_returned_messages(
        self, app_route: Route, offset: int, limit: int | None, returned: list[str]
    ) -> None:
        frames = [f"m{i}" for i in range(5)]

        result = await _collect(
            app_route,
            *frames,
            message_offset=offset,
            message_limit=limit,
            summarize=False,
        )

        assert result["messages"] == returned
        if offset > 0 or limit is not None:
            assert result["pagination"]["offset"] == max(offset, 0)
            assert result["pagination"]["returned"] == len(returned)
        else:
            assert "pagination" not in result


class TestSummarize:
    async def test_a_long_run_of_config_lines_is_elided_with_context(
        self, core_route: Route
    ) -> None:
        frames = [
            "INFO Reading configuration...",
            *YAML,
            "INFO Configuration is valid!",
        ]

        result = await _collect(core_route, *frames)

        messages = result["messages"]
        assert messages[:3] == frames[:3]
        assert messages[3]["elided"] == len(YAML) - 4
        assert "summarize=False" in messages[3]["note"]
        assert messages[4:] == frames[-3:]
        assert result["summary"]["elided_count"] == len(YAML) - 4

    async def test_a_short_run_of_config_lines_is_kept(self, core_route: Route) -> None:
        result = await _collect(core_route, *YAML[:9])

        assert result["messages"] == YAML[:9]
        assert "summary" not in result

    async def test_a_log_line_splits_a_run(self, core_route: Route) -> None:
        frames = [*YAML[:5], "INFO something happened", *YAML[5:10]]

        result = await _collect(core_route, *frames)

        assert result["messages"] == frames

    async def test_an_esphome_validate_stream_keeps_its_signals(
        self, core_route: Route
    ) -> None:
        dump = [f"    key_{i}: val_{i}" for i in range(100)]
        exit_event = {"event": "exit", "code": 0}
        frames = [
            "INFO Reading configuration motion1.yaml...",
            *dump,
            "INFO Configuration is valid!",
            json.dumps(exit_event),
        ]

        result = await _collect(core_route, *frames)

        messages = result["messages"]
        assert messages[0] == frames[0]
        assert messages[3]["elided"] == len(dump) - 4
        assert messages[-2:] == ["INFO Configuration is valid!", exit_event]

    async def test_summarize_off_returns_the_raw_stream(
        self, core_route: Route
    ) -> None:
        result = await _collect(core_route, *YAML, summarize=False)

        assert result["messages"] == YAML
        assert "summary" not in result

    async def test_a_transform_filters_mixed_messages(self, core_route: Route) -> None:
        frames = [
            '{"level": "INFO", "msg": "start"}',
            "raw text",
            '{"level": "ERROR", "msg": "boom"}',
            "another line",
        ]

        result = await _collect(
            core_route,
            *frames,
            summarize=False,
            python_transform="response = [m for m in response if 'ERROR' in str(m) or 'raw' in str(m)]",
        )

        assert result["messages"] == ["raw text", {"level": "ERROR", "msg": "boom"}]
        assert (result["transformed"], result["pre_transform_message_count"]) == (
            True,
            4,
        )
        assert result["message_count"] == 2


class TestFailedConnections:
    async def test_an_app_that_never_completes_the_handshake_times_out(
        self, hass: HomeAssistant, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.hang = True
        before = timers(hass)
        call = asyncio.create_task(
            manage(
                core_route.client,
                slug=SLUG,
                path="/ws",
                websocket=True,
                port=core_route.target.port,
            )
        )
        await core_route.target.arrived.wait()

        await outwait(hass, 5, 11, before)

        body = await rejected_call_body(call)
        assert body["error"]["code"] == "TIMEOUT_OPERATION"
        assert "check its status before retrying" in body["error"]["suggestion"]

    async def test_an_app_that_closes_before_the_first_message_is_reported(
        self, app_route: Route
    ) -> None:
        body = await rejected_call_body(_collect(app_route, body={"type": "spawn"}))

        assert "closed unexpectedly" in body["error"]["message"]

    async def test_inside_the_app_a_refused_handshake_points_at_the_ingress_headers(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.running()
        app_route.target.ws_status = 401

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, path="/ws", websocket=True)
        )

        suggestions = body["error"]["suggestions"]
        assert "rejected the ingress-port handshake" in suggestions[0]
        assert "Status 401" in suggestions[1]

    @pytest.mark.parametrize(
        ("status", "front_door", "offered"), [(401, "absent", True), (403, True, False)]
    )
    async def test_a_refused_direct_port_handshake_offers_the_front_door_only_when_shut(
        self, core_route: Route, status: int, front_door: object, offered: bool
    ) -> None:
        schema = [{"name": "leave_front_door_open", "type": "bool"}]
        options = (
            {} if front_door == "absent" else {"leave_front_door_open": front_door}
        )
        core_route.supervisor.running(options=options, schema=schema)
        core_route.target.ws_status = status

        body = await rejected_call_body(
            manage(
                core_route.client,
                slug=SLUG,
                path="/ws",
                websocket=True,
                port=core_route.target.port,
            )
        )

        primary = body["error"]["suggestions"][0]
        assert ("options={'leave_front_door_open': True}" in primary) is offered
        assert ("app's own authentication" in primary) is not offered

    async def test_a_missing_websocket_path_keeps_the_path_hint(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running()
        core_route.target.ws_status = 404

        body = await rejected_call_body(
            manage(
                core_route.client,
                slug=SLUG,
                path="/nowhere",
                websocket=True,
                port=core_route.target.port,
            )
        )

        assert "handshake failed" in body["error"]["message"]
        assert any(
            "supports WebSocket on this path" in s for s in body["error"]["suggestions"]
        )
