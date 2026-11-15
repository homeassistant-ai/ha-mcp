"""What ``ha_get_logs(source="supervisor"|"system_service")`` returns.

The tool reads an app's or a system service's log from Supervisor by one of two
routes: directly, inside the app, or through Core's ``/api/hassio/...`` proxy
(``_supervisor_logs_core.py``). Each test runs on both, against the server's
real client on a real ``hass``, and checks what the tool returns and what
Supervisor was asked. What it says when Supervisor refuses is in
``test_tools_utility_supervisor_logs_errors.py``.
"""

import re
from pathlib import Path

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
)

from ._supervisor_logs_core import (
    SUPERVISOR_TOKEN,
    Route,
    Target,
    ask,
)
from ._supervisor_logs_core import app_route as app_route
from ._supervisor_logs_core import core_proxy as core_proxy
from ._supervisor_logs_core import core_route as core_route
from ._supervisor_logs_core import route as route
from ._supervisor_logs_core import target as target
from ._tool_errors import error_body

SERVICES = ["supervisor", "host", "core", "dns", "audio", "multicast"]


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
