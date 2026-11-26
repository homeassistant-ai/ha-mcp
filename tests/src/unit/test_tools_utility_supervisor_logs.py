"""What ``ha_get_logs(source="supervisor"|"system_service")`` returns.

The tool reads an app's or a system service's log from Supervisor by one of two
routes: directly, inside the app, or through Core's ``/api/hassio/...`` proxy
(``_supervisor_logs_core.py``). Each test runs on both, against the server's
real client on a real ``hass``, and checks what the tool returns and what
Supervisor was asked. It also covers what the tool says when Supervisor
refuses, answers badly or does not answer, and the ``slug`` parameter and
the error log beside the Supervisor logs (``source="error_log"`` reaches
Core's log by three routes, two of them through Supervisor).
"""

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
)
from ha_mcp.tools.tools_logs import LogTools

from ._core_client import refused_url
from ._log_tool_calls import call_kwargs, set_up_core_logs
from ._supervisor_logs_core import (
    SUPERVISOR_TOKEN,
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
from ._tool_errors import error_body, rejected_call_body

SERVICES = ["supervisor", "host", "core", "dns", "audio", "multicast"]
APP = TARGETS["app"]
SERVICE = TARGETS["service"]
LOG_LINE = "2026-11-05 10:00:00.000+00:00 ERROR (MainThread) [a.b.c] boom"


@pytest.fixture
def error_log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves at ``/api/error_log``, as its logging setup names it.

    Core registers the route only when the file is named, so a test takes this
    before ``core_client``.
    """
    log = tmp_path / "home-assistant.log"
    log.write_text(LOG_LINE + "\n", encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(log)
    return log


@pytest.fixture
async def core_logs(hass: HomeAssistant, error_log_file: Path) -> None:
    """Core's ``system_log`` and ``logger``, which the other sources read."""
    await set_up_core_logs(hass)


async def read(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    return await LogTools(client).get_logs(**call_kwargs(**overrides))


async def refusal(route: Route, target: Target, **overrides: object) -> dict:
    """The body of the error the tool raises for this read."""
    body = await rejected_call_body(ask(route.client, target, **overrides))
    error: dict = body["error"]
    error["context"] = {k: v for k, v in body.items() if k != "error"}
    return error


class TestTheTail:
    async def test_the_newest_line_comes_first_with_its_counts(
        self, route: Route, target: Target
    ):
        route.supervisor.reply("line 1\nline 2\nline 3\n")

        result = await ask(route.client, target)

        assert result["log"] == "line 3\nline 2\nline 1"
        assert (result["source"], result["slug"]) == (target.source, target.slug)
        assert result["total_lines"] == result["returned_lines"] == 3
        assert "filters_applied" not in result
        assert not any("slug" in w for w in result.get("warnings", []))
        assert [r.path for r in route.supervisor.requests] == [target.path]

    async def test_a_limit_keeps_the_newest_lines(self, route: Route, target: Target):
        route.supervisor.reply("\n".join(f"line {i}" for i in range(1, 21)) + "\n")

        result = await ask(route.client, target, limit=5)

        assert result["log"].splitlines() == [f"line {i}" for i in range(20, 15, -1)]
        assert (result["total_lines"], result["returned_lines"]) == (20, 5)
        assert result["limit"] == 5

    async def test_a_search_matches_case_insensitively_and_is_reported(
        self, route: Route, target: Target
    ):
        route.supervisor.reply(
            "INFO startup complete\n"
            "ERROR something broke\n"
            "DEBUG trivial\n"
            "ERROR another failure\n"
        )

        result = await ask(route.client, target, search="error")

        assert result["log"].splitlines() == [
            "ERROR another failure",
            "ERROR something broke",
        ]
        assert result["total_lines"] == 2
        assert result["filters_applied"] == {"search": "error"}

    async def test_a_log_of_json_lines_is_returned_as_text(
        self, route: Route, target: Target
    ):
        """#950: a text/plain log body must not be decoded as JSON."""
        line = '{"level": "info", "msg": "ready"}'
        route.supervisor.reply(line + "\n")

        assert (await ask(route.client, target))["log"] == line


class TestTheWindowAskedOfSupervisor:
    """Supervisor serves 100 lines unless asked for more (found via #1721)."""

    @pytest.mark.parametrize(
        ("overrides", "lines"),
        [
            ({}, DEFAULT_LOG_LIMIT),
            ({"limit": 500}, 500),
            ({"limit": 300}, 300),
            # A search needs history behind the lines it shows.
            ({"limit": 10, "search": "error"}, SUPERVISOR_SEARCH_WINDOW_LINES),
            # The response stays bounded however much is asked for.
            ({"limit": MAX_LIMIT + 2000}, MAX_LIMIT),
        ],
    )
    async def test_the_tail_is_sized_to_the_limit(
        self, route: Route, target: Target, overrides: dict[str, object], lines: int
    ):
        route.supervisor.reply("line\n")

        await ask(route.client, target, **overrides)

        assert route.supervisor.requests[0].query["lines"] == str(lines)

    @pytest.mark.parametrize(("lines", "sent"), [(2000, "2000"), (None, None)])
    async def test_an_app_log_read_names_its_lines_only_when_given(
        self, route: Route, lines: int | None, sent: str | None
    ):
        route.supervisor.reply("x\n")

        await route.client.get_addon_logs("core_ssh", lines=lines)

        query = route.supervisor.requests[0].query
        assert query.get("lines") == sent
        assert query["no_colors"] == "true"


class TestTheRoute:
    async def test_the_app_asks_supervisor_with_its_own_token_for_text(
        self, app_route: Route, target: Target
    ):
        app_route.supervisor.reply("x\n")

        await ask(app_route.client, target)

        (seen,) = app_route.supervisor.requests
        assert seen.authorization == f"Bearer {SUPERVISOR_TOKEN}"
        assert seen.accept == "text/plain"

    async def test_outside_the_app_core_asks_supervisor_and_not_with_the_users_token(
        self, core_route: Route, target: Target
    ):
        core_route.supervisor.reply("x\n")

        await ask(core_route.client, target)

        (seen,) = core_route.supervisor.requests
        assert seen.path == target.path
        assert core_route.client.token not in (seen.authorization or "")

    @pytest.mark.parametrize("service", SERVICES)
    async def test_every_allowed_service_is_read_from_its_own_path(
        self, route: Route, service: str
    ):
        route.supervisor.reply("ok\n")
        service_target = Target("system_service", service, f"/{service}/logs")

        result = await ask(route.client, service_target)

        assert result["slug"] == service
        assert [r.path for r in route.supervisor.requests] == [service_target.path]


class TestTheSlugParameter:
    async def test_a_service_outside_the_allowed_set_is_refused_before_asking(
        self, app_route: Route
    ):
        with pytest.raises(ToolError) as refused:
            await ask(
                app_route.client,
                Target("system_service", "not_a_real_service", ""),
            )

        error = error_body(refused)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        # The caller sees what is allowed.
        listed = error["message"] + " ".join(error.get("suggestions", []))
        assert all(service in listed for service in SERVICES)
        assert app_route.supervisor.requests == []

    @pytest.mark.parametrize("slug", ["x/../../../states?", "a/b", ".", ".."])
    async def test_an_app_slug_that_could_leave_its_path_is_refused_before_asking(
        self, route: Route, slug: str
    ):
        """The slug is one segment of the app's log path, checked as the app tools
        check theirs: on the proxy route Core would read ``x/../../../states?``
        as its states, answer ``a/b`` with 401 and forward ``.`` to Supervisor."""
        route.supervisor.reply("app line\n")

        with pytest.raises(ToolError) as refused:
            await ask(route.client, APP, slug=slug)

        assert error_body(refused)["error"]["code"] == "VALIDATION_FAILED"
        assert route.supervisor.requests == []

    async def test_a_service_log_with_no_slug_is_refused_before_asking(
        self, app_route: Route
    ):
        with pytest.raises(ToolError) as refused:
            await ask(app_route.client, Target("system_service", "", ""), slug=None)

        error = error_body(refused)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "slug" in error["message"]
        assert app_route.supervisor.requests == []

    @pytest.mark.parametrize("source", ["supervisor", "system_service"])
    async def test_level_is_flagged_as_ignored_on_a_container_log(
        self, app_route: Route, source: str
    ):
        """A container's stdout has no level to filter on."""
        app_route.supervisor.reply("x\n")

        result = await ask(app_route.client, Target(source, "host", ""), level="ERROR")

        assert result["success"] is True
        assert any("level" in w and source in w for w in result.get("warnings", [])), (
            result.get("warnings")
        )


class TestStaleToolNameReferences:
    """Regression guard for #950 bug 2: stale `ha_list_addons()` suggestions."""

    def test_no_tool_module_references_removed_ha_list_addons(self):
        """`ha_list_addons` was consolidated into `ha_get_app` — no stale refs.

        Scans every `src/ha_mcp/tools/**/*.py` with a word-boundary regex so
        the guard catches regressions in any module, not just tools_utility.py,
        and ignores substrings inside longer identifiers.
        """
        tools_dir = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"
        pattern = re.compile(r"\bha_list_addons\b")
        offenders = [
            f.relative_to(tools_dir)
            for f in tools_dir.rglob("*.py")
            if pattern.search(f.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            f"Stale `ha_list_addons` reference in: {offenders}. "
            "Replace suggestions/docs with `ha_get_app()` — see #950."
        )


# -- failures ---------------------------------------------------------------


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


# -- slug and the error log -------------------------------------------------


class TestSlugOnAnotherSource:
    @pytest.mark.usefixtures("core_logs")
    @pytest.mark.parametrize("source", ["system", "error_log", "logger"])
    async def test_a_slug_is_flagged_as_ignored(
        self, source: str, core_client: HomeAssistantClient
    ):
        result = await read(core_client, source=source, slug="core_mosquitto")

        assert any("slug" in w and source in w for w in result.get("warnings", [])), (
            result.get("warnings")
        )

    async def test_a_slug_is_flagged_as_ignored_on_the_logbook(
        self,
        recorder_mock: Recorder,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
    ):
        assert await async_setup_component(hass, "logbook", {})

        result = await read(core_client, source="logbook", slug="core_mosquitto")

        assert any(
            "slug" in w and "logbook" in w for w in result.get("warnings", [])
        ), result.get("warnings")

    async def test_a_slug_on_a_supervisor_log_is_not_flagged(
        self, app_route: Route, target: Target
    ):
        """The twin: the warning must not fire where ``slug`` is the point."""
        app_route.supervisor.reply("line\n")

        result = await ask(app_route.client, target)

        assert not any("slug" in w for w in result.get("warnings", []))


class TestTheErrorLogWindow:
    """Both Supervisor-backed routes read the same window, and only that much.

    An unconditional 20,000-line fetch made ``source='error_log'`` hang for
    15+ minutes on a Supervisor-backed install (#2279).
    """

    @pytest.fixture(params=["app", "supervised"])
    def supervisor_route(self, request: pytest.FixtureRequest) -> Route:
        found: Route = request.getfixturevalue(
            "app_route" if request.param == "app" else "core_route"
        )
        return found

    @pytest.mark.parametrize(
        ("limit", "offset", "window"),
        [(100, 0, "entries=:-99:100"), (250, 40, "entries=:-289:250")],
    )
    async def test_the_window_asked_of_supervisor_is_the_one_requested(
        self,
        supervisor_route: Route,
        limit: int,
        offset: int,
        window: str,
    ):
        supervisor_route.supervisor.reply(LOG_LINE + "\n")

        page = await supervisor_route.client.get_error_log(lines=limit, offset=offset)

        (seen,) = supervisor_route.supervisor.requests
        assert seen.path.endswith("/core/logs")
        assert seen.range == window
        assert LOG_LINE in page.text

    @pytest.mark.usefixtures("error_log_file")
    async def test_a_container_install_reads_cores_own_error_log(
        self, core_client: HomeAssistantClient
    ):
        result = await read(core_client, source="error_log")

        assert LOG_LINE in result["log"]
