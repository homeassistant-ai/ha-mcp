"""``ha_get_app``'s reads and ``ha_manage_app``'s config mode.

Each test runs the tool against the stand-in Supervisor
(``_tools_addons_core.py``) outside the app: an app's details and its log
level, the installed and available listings with their statistics, the
options write that merges with what the app already has, and the
``redact_secrets`` toggle on both (issue 2157).
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.redaction import REDACTED_EMPTY, REDACTED_SET, known_secret_values

from ._tool_errors import rejected_call_body
from ._tools_addons_core import SLUG, Route, manage, read
from ._tools_addons_core import core_route as core_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import fresh_secrets as fresh_secrets
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

INFO = f"/addons/{SLUG}/info"
OPTIONS = f"/addons/{SLUG}/options"
LIVE_SECRET = "github_pat_LIVESECRETVALUE"
SECRET_SCHEMA = [
    {"name": "github_pat", "type": "string", "format": "password"},
    {"name": "log_level", "type": "list", "options": ["info", "debug"]},
    {"name": "empty_pw", "type": "string", "format": "password"},
]
SECRET_OPTIONS = {
    "github_pat": LIVE_SECRET,
    "log_level": "info",
    "empty_pw": "",
    "unlisted": "no schema entry",
}


def _installed(slug: str, state: str) -> dict[str, Any]:
    return {"name": slug.title(), "slug": slug, "version": "1.0", "state": state}


class TestAnAppsDetails:
    async def test_the_configured_log_level_is_surfaced(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(options={"log_level": "debug"})

        result = await read(core_route.client, slug=SLUG)

        assert result["log_level"] == "debug"

    @pytest.mark.parametrize("configured", ["", 42])
    async def test_an_unusable_log_level_falls_back_to_the_schema_default(
        self, core_route: Route, configured: object
    ) -> None:
        core_route.supervisor.running(
            options={"log_level": configured},
            schema=[{"name": "log_level", "type": "list(info|debug)"}],
        )

        result = await read(core_route.client, slug=SLUG)

        assert result["log_level"] == "default"

    async def test_an_app_without_a_log_level_reports_none(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(
            options={"port": 1883}, schema=[{"name": "port", "type": "port"}]
        )

        result = await read(core_route.client, slug=SLUG)

        assert "log_level" not in result


class TestListings:
    async def test_the_available_listing_reports_the_installable_version(
        self, core_route: Route
    ) -> None:
        """A store entry's ``version`` is the installed one, null if absent."""
        core_route.supervisor.answer(
            "GET",
            "/store",
            {
                "repositories": [],
                "addons": [
                    {
                        "slug": "core_mosquitto",
                        "version_latest": "6.5.2",
                        "version": "6.5.1",
                    },
                    {"slug": "core_never", "version_latest": "1.4.0", "version": None},
                ],
            },
        )

        result = await read(core_route.client, source="available")

        by_slug = {entry["slug"]: entry for entry in result["addons"]}
        assert by_slug["core_never"]["version"] == "1.4.0"
        assert by_slug["core_never"]["version_installed"] is None
        assert by_slug["core_mosquitto"]["version"] == "6.5.2"
        assert by_slug["core_mosquitto"]["version_installed"] == "6.5.1"

    async def test_statistics_come_from_the_running_apps_only(
        self, core_route: Route
    ) -> None:
        sup = core_route.supervisor
        apps = [_installed("matter", "started"), _installed("parked", "stopped")]
        sup.answer("GET", "/addons", {"addons": apps})
        sup.answer(
            "GET", "/addons/matter/stats", {"cpu_percent": 0.5, "memory_usage": 9}
        )

        result = await read(core_route.client, include_stats=True)

        stats = {entry["slug"]: entry["stats"] for entry in result["addons"]}
        assert stats["matter"]["cpu_percent"] == 0.5
        assert stats["matter"]["memory_usage"] == 9
        assert stats["parked"] is None
        assert ("GET", "/addons/parked/stats") not in sup.requests

    async def test_one_apps_failed_statistics_leave_the_others(
        self, core_route: Route
    ) -> None:
        sup = core_route.supervisor
        apps = [_installed("matter", "started"), _installed("music", "started")]
        sup.answer("GET", "/addons", {"addons": apps})
        sup.refuse("GET", "/addons/matter/stats", "Connection reset")
        sup.answer("GET", "/addons/music/stats", {"memory_percent": 10.8})

        result = await read(core_route.client, include_stats=True)

        stats = {entry["slug"]: entry["stats"] for entry in result["addons"]}
        assert stats["matter"] is None
        assert stats["music"]["memory_percent"] == 10.8
        [warning] = result["warnings"]
        assert warning.startswith("Statistics unavailable for app 'matter'")

    @pytest.mark.parametrize("slug", [None, "../supervisor"])
    async def test_a_running_app_without_a_valid_slug_is_never_asked_for_stats(
        self, core_route: Route, slug: str | None
    ) -> None:
        """Malformed Supervisor data cannot become an ``/addons/None`` request."""
        app = _installed("x", "started") | {"slug": slug}
        core_route.supervisor.answer("GET", "/addons", {"addons": [app]})

        with pytest.raises(TypeError, match="running app without a valid slug"):
            await read(core_route.client, include_stats=True)

        assert core_route.supervisor.requests == [("GET", "/addons")]

    async def test_a_listing_without_statistics_carries_none(
        self, core_route: Route
    ) -> None:
        apps = [_installed("matter", "started")]
        core_route.supervisor.answer("GET", "/addons", {"addons": apps})

        result = await read(core_route.client)

        assert "stats" not in result["addons"][0]
        assert core_route.supervisor.requests == [("GET", "/addons")]


class TestConfigMode:
    async def test_options_are_merged_with_the_current_ones_before_the_write(
        self, core_route: Route
    ) -> None:
        """Supervisor validates the full options, so a partial write would fail."""
        core_route.supervisor.running(
            options={"required_key": "kept", "log_level": "info"},
            schema=[{"name": "required_key"}, {"name": "log_level"}],
        )
        core_route.supervisor.answer("POST", OPTIONS, {})

        result = await manage(
            core_route.client, slug=SLUG, options={"log_level": "debug"}
        )

        assert core_route.supervisor.sent("POST", OPTIONS) == [
            {"options": {"required_key": "kept", "log_level": "debug"}}
        ]
        assert result["status"] == "pending_restart"
        assert result["submitted_fields"] == ["options"]
        assert "ignored_fields" not in result

    async def test_a_nested_option_keeps_its_siblings(self, core_route: Route) -> None:
        core_route.supervisor.running(
            options={
                "ssh": {"sftp": False, "authorized_keys": ["key1"]},
                "log_level": "info",
            },
            schema=[{"name": "ssh"}, {"name": "log_level"}],
        )
        core_route.supervisor.answer("POST", OPTIONS, {})

        await manage(core_route.client, slug=SLUG, options={"ssh": {"sftp": True}})

        [sent] = core_route.supervisor.sent("POST", OPTIONS)
        assert sent["options"] == {
            "ssh": {"sftp": True, "authorized_keys": ["key1"]},
            "log_level": "info",
        }

    async def test_an_option_the_schema_lacks_is_dropped_and_reported(
        self, core_route: Route
    ) -> None:
        """Supervisor drops an unknown option silently; the caller is told."""
        core_route.supervisor.running(
            options={"log_level": "info"}, schema=[{"name": "log_level"}]
        )
        core_route.supervisor.answer("POST", OPTIONS, {})

        result = await manage(
            core_route.client,
            slug=SLUG,
            options={"log_level": "debug", "zombie_field": "ghost"},
        )

        assert core_route.supervisor.sent("POST", OPTIONS) == [
            {"options": {"log_level": "debug"}}
        ]
        assert result["ignored_fields"] == ["zombie_field"]
        assert any(
            "not in app (add-on) schema were ignored" in w for w in result["warnings"]
        )

    async def test_settings_that_need_no_restart_are_written_as_given(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.answer("POST", OPTIONS, {})

        result = await manage(
            core_route.client,
            slug=SLUG,
            boot="manual",
            auto_update=False,
            watchdog=True,
        )

        assert core_route.supervisor.requests == [("POST", OPTIONS)]
        assert core_route.supervisor.sent("POST", OPTIONS) == [
            {"boot": "manual", "auto_update": False, "watchdog": True}
        ]
        assert result["success"] is True

    async def test_a_port_mapping_needs_a_restart(self, core_route: Route) -> None:
        core_route.supervisor.answer("POST", OPTIONS, {})

        result = await manage(core_route.client, slug=SLUG, network={"5800/tcp": 8082})

        assert core_route.supervisor.sent("POST", OPTIONS) == [
            {"network": {"5800/tcp": 8082}}
        ]
        assert result["status"] == "pending_restart"

    async def test_every_config_field_goes_in_one_write(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(schema=[{"name": "log_level"}])
        core_route.supervisor.answer("POST", OPTIONS, {})

        result = await manage(
            core_route.client,
            slug=SLUG,
            options={"log_level": "debug"},
            boot="manual",
            auto_update=False,
            watchdog=True,
            network={"8080/tcp": 9090},
        )

        assert core_route.supervisor.requests == [("GET", INFO), ("POST", OPTIONS)]
        fields = {"options", "boot", "auto_update", "watchdog", "network"}
        assert set(core_route.supervisor.sent("POST", OPTIONS)[0]) == fields
        assert set(result["submitted_fields"]) == fields

    @pytest.mark.parametrize(
        ("overrides", "named"),
        [
            ({"path": "/api/events", "options": {"k": "v"}}, "Cannot combine"),
            ({}, "Must provide either 'path'"),
            ({"path": ""}, "'path' must not be empty"),
            ({"path": "/flows", "method": "INVALID"}, "Invalid HTTP method"),
            ({"path": "/flows", "message_limit": 10}, "WebSocket mode"),
            ({"auto_update": False, "method": "DELETE"}, "method="),
        ],
        ids=[
            "path-and-config",
            "neither",
            "empty-path",
            "bad-method",
            "ws-param",
            "proxy-param",
        ],
    )
    async def test_an_ambiguous_or_invalid_call_is_refused(
        self,
        unreachable_client: HomeAssistantClient,
        overrides: dict[str, Any],
        named: str,
    ) -> None:
        body = await rejected_call_body(
            manage(unreachable_client, slug=SLUG, **overrides)
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert named in body["error"]["message"]


class TestRedaction:
    """``redact_secrets`` hides an app's password options on read and keeps a
    redacted read from being written back."""

    async def test_password_options_are_hidden_and_the_schema_kept(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(options=SECRET_OPTIONS, schema=SECRET_SCHEMA)

        result = await read(core_route.client, slug=SLUG)

        assert result["addon"]["options"] == {
            "github_pat": REDACTED_SET,
            "log_level": "info",
            "empty_pw": REDACTED_EMPTY,
            "unlisted": "no schema entry",
        }
        assert result["addon"]["schema"] == SECRET_SCHEMA

    async def test_a_read_secret_is_remembered_for_the_scrub(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(options=SECRET_OPTIONS, schema=SECRET_SCHEMA)

        await read(core_route.client, slug=SLUG)

        assert LIVE_SECRET in known_secret_values()

    async def test_without_redaction_the_app_is_returned_as_sent(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(options=SECRET_OPTIONS, schema=SECRET_SCHEMA)

        result = await read(core_route.client, slug=SLUG)

        assert result["addon"]["options"] == SECRET_OPTIONS

    @pytest.mark.parametrize("redacting", [True, False])
    async def test_a_redaction_placeholder_is_never_written_back(
        self, core_route: Route, request: pytest.FixtureRequest, redacting: bool
    ) -> None:
        """Also with redaction off: a placeholder read while it was on must
        not overwrite the credential."""
        if redacting:
            request.getfixturevalue("redact_on")

        body = await rejected_call_body(
            manage(core_route.client, slug=SLUG, options={"github_pat": REDACTED_SET})
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "github_pat" in body["error"]["message"]
        assert core_route.supervisor.requests == []

    async def test_a_write_keeps_the_live_secret_and_remembers_it(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(options=SECRET_OPTIONS, schema=SECRET_SCHEMA)
        core_route.supervisor.answer("POST", OPTIONS, {})

        await manage(core_route.client, slug=SLUG, options={"log_level": "debug"})

        [sent] = core_route.supervisor.sent("POST", OPTIONS)
        assert sent["options"]["github_pat"] == LIVE_SECRET
        assert LIVE_SECRET in known_secret_values()

    async def test_a_newly_written_password_is_remembered(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(options=SECRET_OPTIONS, schema=SECRET_SCHEMA)
        core_route.supervisor.answer("POST", OPTIONS, {})

        await manage(
            core_route.client, slug=SLUG, options={"github_pat": "github_pat_BRANDNEW"}
        )

        # The replaced value may still be in logs captured before the write.
        assert {"github_pat_BRANDNEW", LIVE_SECRET} <= set(known_secret_values())

    async def test_a_password_log_level_surfaces_only_its_placeholder(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(
            options={"log_level": "secret-log-pw"},
            schema=[{"name": "log_level", "type": "string", "format": "password"}],
        )

        result = await read(core_route.client, slug=SLUG)

        assert result["log_level"] == REDACTED_SET
        assert result["addon"]["options"]["log_level"] == REDACTED_SET

    async def test_an_app_without_a_schema_has_every_option_hidden(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(
            options={"maybe_pw": "hunter2secret"}, schema=None
        )

        result = await read(core_route.client, slug=SLUG)

        assert result["addon"]["options"] == {"maybe_pw": REDACTED_SET}
        assert len(result["warnings"]) == 1

    async def test_an_app_without_options_needs_no_warning(
        self, core_route: Route, redact_on: None, fresh_secrets: None
    ) -> None:
        core_route.supervisor.running(options={}, schema=None)

        result = await read(core_route.client, slug=SLUG)

        assert result["addon"]["options"] == {}
        assert "warnings" not in result
