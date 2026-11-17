"""``ha_get_app``'s reads and ``ha_manage_app``'s config mode.

Each test runs the tool against the stand-in Supervisor
(``_tools_addons_core.py``) outside the app: an app's details and its log
level, the installed and available listings with their statistics, the
options write that merges with what the app already has, and the
``redact_secrets`` toggle on both (issue 2157).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from aiohasupervisor.models import (
    AddonsStats,
    AddonStage,
    AddonState,
    CpuArch,
    InstalledAddon,
    StoreAddon,
    StoreInfo,
)
from aiohasupervisor.models.addons import AddonsList

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


def _field(name: str, type_: str = "string", **extra: Any) -> dict[str, Any]:
    """One entry of an app's ``schema`` as Supervisor renders it for the UI
    (``UiOptions``): a required option of ``type_``."""
    return {"name": name, "required": True, "type": type_} | extra


SECRET_SCHEMA = [
    _field("github_pat", format="password"),
    _field("log_level", "select", options=["info", "debug"]),
    _field("empty_pw", format="password"),
]
SECRET_OPTIONS = {
    "github_pat": LIVE_SECRET,
    "log_level": "info",
    "empty_pw": "",
    "unlisted": "no schema entry",
}


def _installed(slug: str, state: AddonState) -> InstalledAddon:
    return InstalledAddon(
        advanced=False,
        available=True,
        build=False,
        description=f"The {slug} app",
        homeassistant=None,
        icon=False,
        logo=False,
        name=slug.title(),
        repository="local",
        slug=slug,
        stage=AddonStage.STABLE,
        update_available=False,
        url=None,
        version_latest="1.0",
        version="1.0",
        detached=False,
        state=state,
    )


def _store_app(slug: str, version_latest: str, version: str | None) -> StoreAddon:
    """A ``/store`` entry: ``version`` is the installed one, null if absent."""
    return StoreAddon(
        advanced=False,
        available=True,
        build=False,
        description=f"The {slug} app",
        homeassistant=None,
        icon=False,
        logo=False,
        name=slug.title(),
        repository="core",
        slug=slug,
        stage=AddonStage.STABLE,
        update_available=version is not None and version != version_latest,
        url=None,
        version_latest=version_latest,
        version=version,
        arch=[CpuArch.AMD64],
        documentation=False,
        installed=version is not None,
    )


def _stats(**changes: Any) -> dict[str, Any]:
    """``GET /addons/<slug>/stats`` as Supervisor answers it."""
    stats = AddonsStats(
        cpu_percent=0.0,
        memory_usage=0,
        memory_limit=1024,
        memory_percent=0.0,
        network_rx=0,
        network_tx=0,
        blk_read=0,
        blk_write=0,
    )
    return replace(stats, **changes).to_dict()


def _listing(*apps: InstalledAddon) -> dict[str, Any]:
    """``GET /addons`` as Supervisor answers it."""
    return AddonsList(addons=list(apps)).to_dict()


class TestAnAppsDetails:
    async def test_the_configured_log_level_is_surfaced(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(options={"log_level": "debug"})

        result = await read(core_route.client, slug=SLUG)

        assert result["log_level"] == "debug"

    @pytest.mark.parametrize(("type_", "configured"), [("string", ""), ("integer", 42)])
    async def test_an_unusable_log_level_falls_back_to_the_schema_default(
        self, core_route: Route, type_: str, configured: object
    ) -> None:
        core_route.supervisor.running(
            options={"log_level": configured}, schema=[_field("log_level", type_)]
        )

        result = await read(core_route.client, slug=SLUG)

        assert result["log_level"] == "default"

    async def test_an_app_without_a_log_level_reports_none(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.running(
            options={"port": 1883}, schema=[_field("port", "integer")]
        )

        result = await read(core_route.client, slug=SLUG)

        assert "log_level" not in result


class TestListings:
    async def test_the_available_listing_reports_the_installable_version(
        self, core_route: Route
    ) -> None:
        """A store entry's ``version`` is the installed one, null if absent."""
        store = StoreInfo(
            repositories=[],
            addons=[
                _store_app("core_mosquitto", "6.5.2", "6.5.1"),
                _store_app("core_never", "1.4.0", None),
            ],
        )
        core_route.supervisor.answer("GET", "/store", store.to_dict())

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
        sup.answer(
            "GET",
            "/addons",
            _listing(
                _installed("matter", AddonState.STARTED),
                _installed("parked", AddonState.STOPPED),
            ),
        )
        sup.answer(
            "GET", "/addons/matter/stats", _stats(cpu_percent=0.5, memory_usage=9)
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
        sup.answer(
            "GET",
            "/addons",
            _listing(
                _installed("matter", AddonState.STARTED),
                _installed("music", AddonState.STARTED),
            ),
        )
        sup.refuse("GET", "/addons/matter/stats", "Connection reset")
        sup.answer("GET", "/addons/music/stats", _stats(memory_percent=10.8))

        result = await read(core_route.client, include_stats=True)

        stats = {entry["slug"]: entry["stats"] for entry in result["addons"]}
        assert stats["matter"] is None
        assert stats["music"]["memory_percent"] == 10.8
        [warning] = result["warnings"]
        assert warning.startswith("Statistics unavailable for app 'matter'")

    async def test_a_running_app_without_a_valid_slug_is_never_asked_for_stats(
        self, core_route: Route
    ) -> None:
        """A slug that is no path segment cannot become a stats request.

        Supervisor's own slugs never look like this and the model types the
        field as any string, so it is the one field changed on a real entry.
        """
        app = replace(_installed("x", AddonState.STARTED), slug="../supervisor")
        core_route.supervisor.answer("GET", "/addons", _listing(app))

        with pytest.raises(TypeError, match="running app without a valid slug"):
            await read(core_route.client, include_stats=True)

        assert core_route.supervisor.requests == [("GET", "/addons")]

    async def test_a_listing_without_statistics_carries_none(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.answer(
            "GET", "/addons", _listing(_installed("matter", AddonState.STARTED))
        )

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
            schema=[_field("required_key"), _field("log_level")],
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
            schema=[
                {
                    "name": "ssh",
                    "type": "schema",
                    "optional": True,
                    "multiple": False,
                    "schema": [
                        _field("sftp", "boolean"),
                        _field("authorized_keys", multiple=True),
                    ],
                },
                _field("log_level"),
            ],
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
            options={"log_level": "info"}, schema=[_field("log_level")]
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
        core_route.supervisor.running(schema=[_field("log_level")])
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
            schema=[_field("log_level", format="password")],
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
