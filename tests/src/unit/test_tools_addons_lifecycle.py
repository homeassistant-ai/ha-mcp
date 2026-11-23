"""``ha_manage_app``'s lifecycle, store-wide and store-repository modes.

Each test runs the tool against the stand-in Supervisor
(``_tools_addons_core.py``) and checks what Supervisor was asked and what the
tool reports: lifecycle actions and the app's refusal to update itself,
``check_updates``'s store diff, the idempotent repository actions, and the
parameters each mode refuses so a call has one meaning.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohasupervisor.models import AddonStage, StoreInfo
from aiohasupervisor.models.addons import CpuArch, Repository, StoreAddon

from ha_mcp.client.rest_client import HomeAssistantClient

from ._haos_build_core import supervisor_info
from ._supervisor_logs_core import route as route
from ._tool_errors import rejected_call_body
from ._tools_addons_core import SLUG, Route, app_info, manage, read
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_route as core_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

REPO_URL = "https://github.com/example/home-assistant-apps"
CORE_APPS_URL = "https://github.com/home-assistant/apps"
REPOSITORIES = "/store/repositories"


def _app(slug: str, version: str, *, update_available: bool = False) -> StoreAddon:
    """A ``/store`` entry: ``version_latest`` is what installing would get;
    ``version`` is the installed one, None for an app not installed."""
    return StoreAddon(
        advanced=False,
        available=True,
        build=False,
        description="An app in the store",
        homeassistant=None,
        icon=False,
        logo=False,
        name=slug.replace("_", " ").title(),
        repository="core",
        slug=slug,
        stage=AddonStage.STABLE,
        update_available=update_available,
        url=None,
        version_latest=version,
        version=None,
        arch=[CpuArch.AMD64],
        documentation=False,
        installed=False,
    )


def _store(*apps: StoreAddon) -> dict[str, Any]:
    """``GET /store`` as Supervisor answers it, built from its model."""
    return StoreInfo(repositories=[], addons=list(apps)).to_dict()


class TestLifecycleActions:
    async def test_install_posts_to_the_store_with_a_json_body(
        self, route: Route
    ) -> None:
        """Supervisor's install handler parses an optional JSON body."""
        install = f"/store/addons/{SLUG}/install"
        route.supervisor.answer("POST", install, {})

        result = await manage(route.client, slug=SLUG, action="install")

        assert (result["action"], result["slug"]) == ("install", SLUG)
        assert route.supervisor.sent("POST", install) == [{}]

    async def test_start_posts_to_the_app(self, core_route: Route) -> None:
        core_route.supervisor.answer("POST", f"/addons/{SLUG}/start", {})

        result = await manage(core_route.client, slug=SLUG, action="start")

        assert result["action"] == "start"
        assert core_route.supervisor.requests == [("POST", f"/addons/{SLUG}/start")]

    async def test_inside_the_app_updating_itself_is_refused_with_guidance(
        self, app_route: Route
    ) -> None:
        """Supervisor forbids an app updating itself; nothing is sent."""
        app_route.supervisor.answer("GET", "/addons/self/info", app_info(SLUG))

        body = await rejected_call_body(
            manage(app_route.client, slug=SLUG, action="update")
        )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["self_slug"] == SLUG
        assert any("Apps UI" in s for s in body["error"]["suggestions"])
        assert app_route.supervisor.requests == [("GET", "/addons/self/info")]

    async def test_inside_the_app_another_apps_update_goes_ahead(
        self, app_route: Route
    ) -> None:
        update = "/store/addons/core_ssh/update"
        app_route.supervisor.answer("GET", "/addons/self/info", app_info(SLUG))
        app_route.supervisor.answer("POST", update, {})

        result = await manage(app_route.client, slug="core_ssh", action="update")

        assert result["success"] is True
        assert app_route.supervisor.requests == [
            ("GET", "/addons/self/info"),
            ("POST", update),
        ]

    async def test_outside_the_app_an_update_asks_for_no_identity(
        self, core_route: Route
    ) -> None:
        update = "/store/addons/core_ssh/update"
        core_route.supervisor.answer("POST", update, {})

        await manage(core_route.client, slug="core_ssh", action="update")

        assert core_route.supervisor.requests == [("POST", update)]

    async def test_an_unknown_action_is_rejected(
        self, unreachable_client: HomeAssistantClient
    ) -> None:
        body = await rejected_call_body(
            manage(unreachable_client, slug=SLUG, action="frobnicate")
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.parametrize(
        ("call", "parameter"),
        [
            ({"slug": ".."}, "slug"),
            ({"slug": "../../host/reboot?", "action": "start"}, "slug"),
            ({"action": "remove_repository", "repository": "core%2Fssh"}, "repository"),
        ],
        ids=["ha_get_app", "lifecycle", "remove-repository"],
    )
    async def test_a_slug_that_could_leave_its_path_never_reaches_supervisor(
        self, core_route: Route, call: dict[str, Any], parameter: str
    ) -> None:
        tool = read if call.keys() == {"slug"} else manage

        body = await rejected_call_body(tool(core_route.client, **call))

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert body["parameter"] == parameter
        assert core_route.supervisor.requests == []

    @pytest.mark.parametrize(
        ("parameter", "value", "named"),
        [
            ("path", "/info", "path"),
            ("options", {"k": "v"}, "config parameters"),
            ("array_patch", {"operations": []}, "array_patch"),
            ("method", "POST", "method"),
            ("body", {"probe": True}, "body"),
            ("debug", True, "debug"),
            ("port", 8123, "port"),
            ("offset", 1, "offset"),
            ("limit", 1, "limit"),
            ("websocket", True, "websocket"),
            ("wait_for_close", False, "wait_for_close"),
            ("message_limit", 1, "message_limit"),
            ("message_offset", 1, "message_offset"),
            ("summarize", False, "summarize"),
            ("python_transform", "response = response", "python_transform"),
            ("request_headers", {"X-Test": "value"}, "request_headers"),
        ],
    )
    async def test_a_lifecycle_action_refuses_another_modes_parameter(
        self,
        unreachable_client: HomeAssistantClient,
        parameter: str,
        value: Any,
        named: str,
    ) -> None:
        body = await rejected_call_body(
            manage(unreachable_client, slug=SLUG, action="start", **{parameter: value})
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert named in body["error"]["message"]


class TestCheckUpdates:
    """``check_updates`` reloads the store and reports what the reload changed."""

    async def test_it_reloads_the_store(self, core_route: Route) -> None:
        core_route.supervisor.answer("GET", "/store", _store())
        core_route.supervisor.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        assert result["action"] == "check_updates"
        assert core_route.supervisor.requests == [
            ("GET", "/store"),
            ("POST", "/store/reload"),
            ("GET", "/store"),
        ]

    async def test_it_names_the_versions_the_reload_discovered(
        self, core_route: Route
    ) -> None:
        sup = core_route.supervisor
        sup.answer(
            "GET", "/store", _store(_app("local_thing", "1.0.1"), _app("b", "6"))
        )
        sup.answer(
            "GET",
            "/store",
            _store(_app("local_thing", "1.0.3"), _app("b", "6"), _app("new_app", "1")),
        )
        sup.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        changed = {entry["slug"]: entry for entry in result["changed"]}
        assert set(changed) == {"local_thing", "new_app"}
        assert changed["local_thing"]["version_before"] == "1.0.1"
        assert changed["local_thing"]["version_after"] == "1.0.3"
        assert changed["new_app"]["version_before"] is None

    async def test_it_reports_an_app_that_left_the_store(
        self, core_route: Route
    ) -> None:
        """A removed or unreachable repository takes its apps with it."""
        sup = core_route.supervisor
        sup.answer("GET", "/store", _store(_app("kept", "6"), _app("gone_app", "1")))
        sup.answer("GET", "/store", _store(_app("kept", "6")))
        sup.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        assert result["changed"] == [
            {
                "slug": "gone_app",
                "name": "Gone App",
                "version_before": "1",
                "version_after": None,
            }
        ]

    async def test_it_lists_the_apps_with_an_update_and_installs_nothing(
        self, core_route: Route
    ) -> None:
        sup = core_route.supervisor
        sup.answer("GET", "/store", _store())
        sup.answer(
            "GET",
            "/store",
            _store(
                _app("core_mosquitto", "6.5.2", update_available=True), _app("b", "1")
            ),
        )
        sup.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        assert [entry["slug"] for entry in result["updates_available"]] == [
            "core_mosquitto"
        ]
        assert "action='update'" in result["message"]

    async def test_an_unreadable_store_costs_detail_not_the_reload(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.refuse("GET", "/store", "store unavailable")
        core_route.supervisor.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        assert result["success"] is True
        # Null, not []: "0 changed" would claim the store was read.
        assert (result["changed"], result["updates_available"]) == (None, None)
        assert "unknown" in result["message"]
        assert len(result["warnings"]) == 4

    @pytest.mark.parametrize("failing", ["before", "after"])
    async def test_a_half_read_store_is_not_diffed(
        self, core_route: Route, failing: str
    ) -> None:
        """An unread side taken as empty would report every app as new or gone."""
        sup = core_route.supervisor
        read_store = {"before": [False, True], "after": [True, False]}[failing]
        for readable in read_store:
            if readable:
                sup.answer("GET", "/store", _store(_app("a", "6"), _app("b", "1")))
            else:
                sup.refuse("GET", "/store", "store unavailable")
        sup.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        assert result["changed"] is None
        assert any("could not be read" in w for w in result["warnings"])
        expected: list[Any] | None = None if failing == "after" else []
        assert result["updates_available"] == expected

    async def test_a_failed_store_read_carries_its_cause(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.refuse("GET", "/store", "Token is not valid", 401)
        core_route.supervisor.answer("GET", "/store", _store())
        core_route.supervisor.answer("POST", "/store/reload", {})

        result = await manage(core_route.client, action="check_updates")

        assert any("Token is not valid" in w for w in result["warnings"])

    async def test_a_failed_reload_stays_a_failure(self, core_route: Route) -> None:
        core_route.supervisor.answer("GET", "/store", _store())
        core_route.supervisor.refuse("POST", "/store/reload", "connection refused")
        core_route.supervisor.answer("GET", "/supervisor/info", supervisor_info())

        body = await rejected_call_body(
            manage(core_route.client, action="check_updates")
        )

        assert "connection refused" in body["error"]["message"]
        assert "pending update" not in body["error"]["message"]
        assert core_route.supervisor.requests == [
            ("GET", "/store"),
            ("POST", "/store/reload"),
            ("GET", "/supervisor/info"),
        ]

    @pytest.mark.parametrize(
        ("message", "pending"),
        [
            (
                "'StoreManager.reload' blocked from execution, supervisor needs "
                "to be updated first",
                True,
            ),
            ("failed to update app core_mosquitto on supervisor", False),
        ],
        ids=["supervisor-update-pending", "app-update-failed"],
    )
    async def test_only_a_pending_supervisor_update_is_explained_as_one(
        self, core_route: Route, message: str, pending: bool
    ) -> None:
        core_route.supervisor.answer("GET", "/store", _store())
        core_route.supervisor.refuse("POST", "/store/reload", message)
        core_route.supervisor.answer(
            "GET", "/supervisor/info", supervisor_info(update_available=pending)
        )

        body = await rejected_call_body(
            manage(core_route.client, action="check_updates")
        )

        assert ("pending update" in body["error"]["message"]) is pending

    @pytest.mark.parametrize(
        ("overrides", "named"),
        [
            ({"slug": "core_mosquitto"}, "slug"),
            ({"repository": "0f1cc410"}, "repository"),
            ({"path": "/api/status"}, "path"),
            ({"boot": "auto"}, "config parameters"),
            ({"websocket": True}, "websocket"),
        ],
    )
    async def test_it_refuses_a_narrower_scope(
        self,
        unreachable_client: HomeAssistantClient,
        overrides: dict[str, Any],
        named: str,
    ) -> None:
        body = await rejected_call_body(
            manage(unreachable_client, action="check_updates", **overrides)
        )

        assert named in body["error"]["message"]


class TestRepositories:
    async def test_add_posts_the_trimmed_url_without_a_slug(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.answer("POST", REPOSITORIES, {})

        result = await manage(
            core_route.client, action="add_repository", repository=f"  {REPO_URL}  "
        )

        assert (result["action"], result["repository"]) == ("add_repository", REPO_URL)
        assert core_route.supervisor.sent("POST", REPOSITORIES) == [
            {"repository": REPO_URL}
        ]

    async def test_remove_deletes_by_slug_without_a_body(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.answer("DELETE", f"{REPOSITORIES}/0f1cc410", {})

        result = await manage(
            app_route.client, action="remove_repository", repository="0f1cc410"
        )

        assert result["repository"] == "0f1cc410"
        assert app_route.supervisor.sent("DELETE", f"{REPOSITORIES}/0f1cc410") == [None]

    async def test_an_unknown_outcome_survives_the_repository_guidance(
        self, route: Route
    ) -> None:
        route.supervisor.drop("POST", REPOSITORIES)

        body = await rejected_call_body(
            manage(route.client, action="add_repository", repository=REPO_URL)
        )

        assert (body["outcome"], body["method"]) == ("unknown", "POST")
        assert body["endpoint"] == REPOSITORIES

    @pytest.mark.parametrize(
        ("requested", "source", "slug"),
        [
            (REPO_URL, REPO_URL, "abc123"),
            ("https://github.com/home-assistant/addons", "core", "core"),
            (CORE_APPS_URL, "core", "core"),
            ("core", "core", "core"),
        ],
    )
    async def test_adding_a_repository_already_in_the_store_succeeds(
        self, route: Route, requested: str, source: str, slug: str
    ) -> None:
        route.supervisor.refuse(
            "POST", REPOSITORIES, "Repository registration declined"
        )
        repository = Repository(
            slug=slug,
            name="Test apps",
            source=source,
            url=CORE_APPS_URL if source == "core" else source,
            maintainer="Test",
        )
        route.supervisor.answer(
            "GET", "/store", StoreInfo(repositories=[repository], addons=[]).to_dict()
        )

        result = await manage(
            route.client, action="add_repository", repository=requested
        )

        assert result["repository"] == requested
        assert result["success"] is True
        assert "no change needed" in result["message"]

    async def test_direct_repository_conflict_uses_supervisors_error_key(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.refuse(
            "POST",
            REPOSITORIES,
            "Repository registration declined",
            status=409,
            error_key="store_repository_already_added_error",
        )
        result = await manage(
            app_route.client, action="add_repository", repository=REPO_URL
        )
        assert "already registered" in result["message"]
        assert app_route.supervisor.requests == [("POST", REPOSITORIES)]

    async def test_removing_a_repository_not_in_the_store_succeeds(
        self, route: Route
    ) -> None:
        route.supervisor.refuse(
            "DELETE",
            f"{REPOSITORIES}/deadbeef",
            "Repository removal declined",
            404,
        )

        route.supervisor.answer("GET", "/store", _store())
        result = await manage(
            route.client, action="remove_repository", repository="deadbeef"
        )

        assert result["success"] is True
        assert "no change needed" in result["message"]

    async def test_a_not_found_about_something_else_still_fails(
        self, core_route: Route
    ) -> None:
        core_route.supervisor.refuse(
            "DELETE", f"{REPOSITORIES}/x", "App core_foo not found"
        )
        still_listed = Repository(
            slug="x", name="Test apps", source=REPO_URL, url=REPO_URL, maintainer="Test"
        )
        core_route.supervisor.answer(
            "GET", "/store", StoreInfo(repositories=[still_listed], addons=[]).to_dict()
        )

        await rejected_call_body(
            manage(core_route.client, action="remove_repository", repository="x")
        )

    async def test_a_refused_permission_is_not_taken_for_an_existing_repository(
        self, app_route: Route
    ) -> None:
        app_route.supervisor.refuse(
            "POST",
            REPOSITORIES,
            "Denied, though the repository is already in the store",
            403,
        )

        body = await rejected_call_body(
            manage(app_route.client, action="add_repository", repository=REPO_URL)
        )

        assert body["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"

    @pytest.mark.parametrize(
        ("action", "message", "advice"),
        [
            ("add_repository", "invalid repository", "app (add-on) repository URL"),
            (
                "remove_repository",
                "Can't remove repository, it is used by installed apps",
                "uninstalled",
            ),
        ],
    )
    async def test_a_failed_repository_action_gets_repository_advice(
        self, core_route: Route, action: str, message: str, advice: str
    ) -> None:
        """The store cannot be read back to explain the refusal."""
        path = REPOSITORIES if action == "add_repository" else f"{REPOSITORIES}/abc123"
        method = "POST" if action == "add_repository" else "DELETE"
        core_route.supervisor.refuse(method, path, message)
        core_route.supervisor.refuse("GET", "/store", "store unavailable")
        repository = REPO_URL if action == "add_repository" else "abc123"

        body = await rejected_call_body(
            manage(core_route.client, action=action, repository=repository)
        )

        error = body["error"]
        # A single suggestion is given under the singular key.
        suggestions = " ".join(
            [error.get("suggestion", ""), *error.get("suggestions", [])]
        )
        assert advice in suggestions
        assert "connection" not in suggestions.lower()

    @pytest.mark.parametrize(
        ("action", "listed", "message"),
        [
            ("add_repository", "core", "invalid repository"),
            (
                "remove_repository",
                "abc123",
                "Can't remove repository, it is used by installed apps",
            ),
        ],
    )
    async def test_a_refusal_the_store_does_not_explain_still_fails(
        self, core_route: Route, action: str, listed: str, message: str
    ) -> None:
        """The store is readable and shows the refused change is not already
        in place: the add is absent from it, the removed one still listed."""
        adding = action == "add_repository"
        path = REPOSITORIES if adding else f"{REPOSITORIES}/abc123"
        core_route.supervisor.refuse("POST" if adding else "DELETE", path, message)
        stored = Repository(
            slug=listed,
            name="Test apps",
            source="core" if adding else REPO_URL,
            url=CORE_APPS_URL if adding else REPO_URL,
            maintainer="Test",
        )
        core_route.supervisor.answer(
            "GET", "/store", StoreInfo(repositories=[stored], addons=[]).to_dict()
        )

        body = await rejected_call_body(
            manage(
                core_route.client,
                action=action,
                repository=REPO_URL if adding else "abc123",
            )
        )

        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert message in str(body)

    @pytest.mark.parametrize("repository", [None, "   "])
    async def test_a_repository_action_needs_a_repository(
        self, unreachable_client: HomeAssistantClient, repository: str | None
    ) -> None:
        body = await rejected_call_body(
            manage(
                unreachable_client, action="remove_repository", repository=repository
            )
        )

        assert body["parameter"] == "repository"

    @pytest.mark.parametrize(
        ("parameter", "value", "named"),
        [
            ("slug", "core_ssh", "slug"),
            ("path", "/info", "path"),
            ("options", {"k": "v"}, "config parameters"),
            ("method", "POST", "method"),
        ],
    )
    async def test_a_repository_action_refuses_another_modes_parameter(
        self,
        unreachable_client: HomeAssistantClient,
        parameter: str,
        value: Any,
        named: str,
    ) -> None:
        body = await rejected_call_body(
            manage(
                unreachable_client,
                action="add_repository",
                repository=REPO_URL,
                **{parameter: value},
            )
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert named in body["error"]["message"]

    async def test_a_repository_without_a_repository_action_is_refused(
        self, unreachable_client: HomeAssistantClient
    ) -> None:
        body = await rejected_call_body(
            manage(unreachable_client, slug="core_ssh", repository=REPO_URL)
        )

        assert body["parameter"] == "repository"
