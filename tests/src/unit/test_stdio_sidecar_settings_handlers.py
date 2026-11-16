"""The settings handlers the sidecar serves without a live server (#863).

The sidecar builds them with ``server=None``: tools come from the metadata
cache the stdio parent dumped, settings from the data dir. The embedded
(in-process) server builds them with its client, and restarts by reloading its
config entry in the real Home Assistant.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import (
    FEATURE_FLAG_FIELDS,
    get_feature_flag_origin,
    get_global_settings,
    reset_global_settings,
)
from ha_mcp.policy import persistence as policy_persistence
from ha_mcp.settings_ui import (
    SETTINGS_ROUTES,
    build_settings_handlers,
    dump_tool_metadata_cache,
    load_tool_metadata_cache,
)
from ha_mcp.tools import tools_dev
from ha_mcp.utils import config_write_lock
from ha_mcp.visibility import persistence as visibility_persistence

from ._server_entry import add_server_entry
from ._stdio_sidecar_core import data_dir as data_dir

# The override file the settings page writes; users find it by this name.
OVERRIDE_FILE = "feature_flags.json"


def _app(key: str, path: str, *methods: str, **kwargs: Any) -> Starlette:
    handlers = build_settings_handlers(server=kwargs.pop("server", None), **kwargs)
    return Starlette(routes=[Route(path, handlers[key], methods=list(methods))])


def _recording_lock(events: list[str]) -> Any:
    @contextlib.contextmanager
    def lock(data_dir: Path | None = None) -> Iterator[None]:
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")

    return lock


class TestToolMetadataCache:
    def test_dump_and_load(self, data_dir: Path) -> None:
        payload = [{"name": "ha_get_state", "primary_tag": "Entity Operations"}]

        assert dump_tool_metadata_cache(payload) is True
        assert load_tool_metadata_cache() == payload

    def test_load_missing_file_returns_empty(self, data_dir: Path) -> None:
        assert load_tool_metadata_cache() == []

    def test_load_malformed_returns_empty(self, data_dir: Path) -> None:
        (data_dir / "tool_metadata.json").write_text("not json {{{")

        assert load_tool_metadata_cache() == []

    def test_load_wrong_shape_returns_empty(self, data_dir: Path) -> None:
        (data_dir / "tool_metadata.json").write_text('{"not": "a list"}')

        assert load_tool_metadata_cache() == []

    def test_a_dump_that_cannot_be_written_reports_false(self, data_dir: Path) -> None:
        """Stdio startup must not stop on cache I/O; the sidecar shows no tools."""
        (data_dir / "tool_metadata.json").mkdir()

        assert dump_tool_metadata_cache([{"name": "x"}]) is False

    def test_dump_is_atomic(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dump overlaps the old, still-serving sidecar on every startup:
        a truncate-write would hand a concurrent Tools request an empty list."""
        cache = data_dir / "tool_metadata.json"
        cache.write_text('[{"name": "old"}]')
        at_replace: list[str] = []
        real_replace = os.replace

        def observing_replace(src: Any, dst: Any) -> None:
            at_replace.append(Path(dst).read_text())
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", observing_replace)

        assert dump_tool_metadata_cache([{"name": "new"}]) is True
        assert at_replace == ['[{"name": "old"}]']
        assert json.loads(cache.read_text()) == [{"name": "new"}]


class TestSidecarHandlers:
    def test_every_shared_route_has_a_handler(self) -> None:
        """The sidecar mounts SETTINGS_ROUTES from this handler set; the
        add-on root page is the only handler outside that table."""
        handlers = build_settings_handlers(server=None)

        routed = {key for _path, _methods, key in SETTINGS_ROUTES}
        assert set(handlers) == routed | {"root_page"}

    def test_tools_come_from_the_dumped_cache(self, data_dir: Path) -> None:
        payload = [{"name": "ha_call_event", "primary_tag": "System"}]
        dump_tool_metadata_cache(payload)
        app = _app("get_tools", "/api/settings/tools", "GET")

        resp = TestClient(app).get("/api/settings/tools")

        assert resp.status_code == 200
        assert resp.json()["tools"] == payload

    def test_policy_save_holds_the_cross_process_lock(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sidecar is a separate process from the MCP server: without the
        policy tools' file lock (#2148) its version check is no compare-and-swap,
        and a save could overwrite a tool's commit."""
        events: list[str] = []
        real_save = policy_persistence.save_policy

        def recording_save(data_dir: Path, policy: Any) -> None:
            events.append("save")
            real_save(data_dir, policy)

        monkeypatch.setattr(
            config_write_lock, "config_file_lock", _recording_lock(events)
        )
        monkeypatch.setattr(policy_persistence, "save_policy", recording_save)
        app = _app("policy_put_config", "/api/policy/config", "PUT")

        resp = TestClient(app).put(
            "/api/policy/config",
            json={
                "wait_seconds": 60,
                "approval_ttl_minutes": 5,
                "rules": [],
                "version": 0,
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["saved"] is True
        assert events == ["acquire", "save", "release"]

    def test_restart_is_refused_without_a_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with SUPERVISOR_TOKEN inherited, the sidecar has no add-on to restart."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
        app = _app("restart_addon", "/api/settings/restart", "POST")

        assert TestClient(app).post("/api/settings/restart").status_code == 400


class TestFeatureFlags:
    """``/api/settings/features``: env-set flags are locked, the rest editable.

    The page disables env-controlled rows from this answer, so losing the
    lock semantics would let the user "save" a value the env overrides.
    """

    @pytest.fixture(autouse=True)
    def no_flag_env(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for _field, env_name, _type in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        reset_global_settings()

    @staticmethod
    def _get() -> dict[str, Any]:
        app = _app("get_feature_flags", "/api/settings/features", "GET")
        resp = TestClient(app).get("/api/settings/features")
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        return body

    @staticmethod
    def _post(**kwargs: Any) -> Any:
        app = _app("save_feature_flags", "/api/settings/features", "POST")
        return TestClient(app).post("/api/settings/features", **kwargs)

    def test_untouched_flags_are_defaults_and_editable(self) -> None:
        flags = self._get()["flags"]

        for field_name, env_name, _type in FEATURE_FLAG_FIELDS:
            assert flags[field_name]["origin"] == "default", field_name
            assert flags[field_name]["editable"] is True
            assert flags[field_name]["env_var"] == env_name

    def test_an_env_set_flag_is_locked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
        reset_global_settings()

        assert self._get()["flags"]["enable_tool_search"] == {
            "value": True,
            "origin": "env",
            "editable": False,
            "type": "bool",
            "env_var": "ENABLE_TOOL_SEARCH",
        }

    def test_a_save_lands_in_the_file_and_the_live_settings(
        self, data_dir: Path
    ) -> None:
        flags = {"enable_tool_search": True, "tool_search_max_results": 7}

        resp = self._post(json={"flags": flags})

        assert resp.status_code == 200
        assert json.loads((data_dir / OVERRIDE_FILE).read_text()) == flags
        settings = get_global_settings()
        assert settings.enable_tool_search is True
        assert settings.tool_search_max_results == 7

    def test_a_save_leaves_no_temp_file(self, data_dir: Path) -> None:
        resp = self._post(json={"flags": {"enable_tool_search": True}})

        assert resp.status_code == 200
        assert [path.name for path in data_dir.glob(f"{OVERRIDE_FILE}*")] == [
            OVERRIDE_FILE
        ]

    def test_an_env_locked_flag_is_refused_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
        reset_global_settings()

        resp = self._post(json={"flags": {"enable_tool_search": True}})

        assert resp.status_code == 400
        assert "ENABLE_TOOL_SEARCH" in resp.json()["error"]["message"]

    def test_a_corrupt_existing_file_is_not_overwritten(self, data_dir: Path) -> None:
        """Overwriting it would silently erase every toggle the user had set."""
        corrupt = data_dir / OVERRIDE_FILE
        corrupt.write_text("{not json")

        resp = self._post(json={"flags": {"enable_tool_search": True}})

        assert resp.status_code == 409
        assert corrupt.read_text() == "{not json"

    @pytest.mark.parametrize(
        ("request_kwargs", "message_part"),
        [
            pytest.param(
                {
                    "content": b"not json {{{",
                    "headers": {"content-type": "application/json"},
                },
                "Invalid JSON",
                id="not-json",
            ),
            pytest.param({"json": [1, 2, 3]}, None, id="body-not-object"),
            pytest.param(
                {"json": {"flags": ["enable_tool_search"]}}, "object", id="flags-list"
            ),
            pytest.param(
                {"json": {"flags": {"nonexistent_flag": True}}},
                "nonexistent_flag",
                id="unknown-flag",
            ),
            pytest.param(
                {"json": {"flags": {"enable_tool_search": "true"}}},
                None,
                id="text-for-bool",
            ),
            pytest.param(
                {"json": {"flags": {"tool_search_max_results": True}}},
                None,
                id="bool-for-int",
            ),
            pytest.param(
                {"json": {"flags": {"tool_search_max_results": 999}}},
                None,
                id="int-out-of-range",
            ),
        ],
    )
    def test_a_malformed_save_is_a_400_not_a_silent_coercion(
        self, data_dir: Path, request_kwargs: dict[str, Any], message_part: str | None
    ) -> None:
        """``"false"`` is truthy and ``True`` is an int in Python; a misspelled
        flag would be dropped while the page says "Saved"."""
        resp = self._post(**request_kwargs)

        assert resp.status_code == 400
        if message_part is not None:
            assert message_part.lower() in resp.json()["error"]["message"].lower()
        assert not (data_dir / OVERRIDE_FILE).exists()


class TestFeatureFlagFile:
    """How the override file is read outside the add-on and ignored inside it."""

    @pytest.fixture(autouse=True)
    def fresh_settings(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("ENABLE_TOOL_SEARCH", "TOOL_SEARCH_MAX_RESULTS"):
            monkeypatch.delenv(name, raising=False)
        reset_global_settings()

    def test_in_the_addon_the_addon_config_is_the_origin(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """start.py is the only writer of env vars there; a stale file must not
        shadow what the add-on's Configuration tab says."""
        (data_dir / OVERRIDE_FILE).write_text(json.dumps({"enable_tool_search": True}))
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")

        assert get_feature_flag_origin("ENABLE_TOOL_SEARCH") == "addon"

    def test_in_the_addon_a_stale_file_is_ignored(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (data_dir / OVERRIDE_FILE).write_text(
            json.dumps({"enable_tool_search": True, "tool_search_max_results": 9})
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")

        settings = get_global_settings()

        assert settings.enable_tool_search is False
        assert settings.tool_search_max_results == 5

    def test_a_missing_file_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            get_global_settings()

        assert not [r for r in caplog.records if "override file" in r.getMessage()]

    @pytest.mark.parametrize(
        ("content", "warning"),
        [
            pytest.param("{not valid", "not valid JSON", id="not-json"),
            pytest.param("[1,2,3]", "not a JSON object", id="not-an-object"),
        ],
    )
    def test_an_unreadable_file_warns_and_falls_back(
        self,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
        content: str,
        warning: str,
    ) -> None:
        """A user whose toggles silently stop working needs a line to grep for."""
        (data_dir / OVERRIDE_FILE).write_text(content)

        with caplog.at_level(logging.WARNING):
            settings = get_global_settings()

        assert settings.enable_tool_search is False
        assert any(warning in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]


class TestVisibilityHandlers:
    """Entity visibility config GET/PUT (#1728)."""

    @staticmethod
    def _client() -> TestClient:
        handlers = build_settings_handlers(server=None)
        path = "/api/visibility/config"
        app = Starlette(
            routes=[
                Route(path, handlers["visibility_get_config"], methods=["GET"]),
                Route(path, handlers["visibility_put_config"], methods=["PUT"]),
            ]
        )
        return TestClient(app)

    def test_get_returns_default_when_no_file(self, data_dir: Path) -> None:
        body = self._client().get("/api/visibility/config").json()

        assert body["enabled"] is False
        assert body["version"] == 1

    def test_put_persists_and_bumps_version(self, data_dir: Path) -> None:
        client = self._client()
        resp = client.put(
            "/api/visibility/config",
            json={"version": 1, "enabled": True, "deny_entity_ids": ["light.x"]},
        )

        assert resp.status_code == 200
        assert resp.json()["version"] == 2
        got = client.get("/api/visibility/config").json()
        assert (got["enabled"], got["deny_entity_ids"], got["version"]) == (
            True,
            ["light.x"],
            2,
        )

    def test_put_persists_enforce_flag(self, data_dir: Path) -> None:
        """The enforce flag (#2015) round-trips through the generic model."""
        client = self._client()
        client.put(
            "/api/visibility/config",
            json={"version": 1, "enabled": True, "enforce": True},
        )

        assert client.get("/api/visibility/config").json()["enforce"] is True

    def test_put_stale_version_conflicts(self, data_dir: Path) -> None:
        client = self._client()
        client.put("/api/visibility/config", json={"version": 1, "enabled": True})

        resp = client.put(
            "/api/visibility/config", json={"version": 1, "enabled": False}
        )

        assert resp.status_code == 409
        assert resp.json()["current_version"] == 2

    def test_put_rejects_invalid_payload(self, data_dir: Path) -> None:
        resp = self._client().put("/api/visibility/config", json={"version": "abc"})

        assert resp.status_code == 400

    def test_put_holds_the_cross_process_lock(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sidecar's compare-and-save must be one locked step."""
        events: list[str] = []
        real_save = visibility_persistence.save_visibility_config

        def recording_save(data_dir: Path, config: Any) -> None:
            events.append("save")
            real_save(data_dir, config)

        monkeypatch.setattr(
            config_write_lock, "config_file_lock", _recording_lock(events)
        )
        monkeypatch.setattr(
            visibility_persistence, "save_visibility_config", recording_save
        )

        resp = self._client().put(
            "/api/visibility/config",
            json={"version": 1, "enabled": True, "deny_entity_ids": ["light.x"]},
        )

        assert resp.status_code == 200, resp.text
        assert events == ["acquire", "save", "release"]


class TestEmbeddedRestart:
    """The in-process server restarts by reloading its own config entry."""

    @pytest.fixture
    async def embedded(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        """Embedded mode; returns the entries a deferred reload was scheduled for."""
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert await async_setup_component(hass, "config", {})
        scheduled: list[str] = []
        monkeypatch.setattr(
            tools_dev,
            "schedule_deferred_entry_reload",
            lambda _client, entry_id: scheduled.append(entry_id),
        )
        return scheduled

    @staticmethod
    async def _restart(client: HomeAssistantClient) -> httpx.Response:
        app = _app(
            "restart_addon",
            "/api/settings/restart",
            "POST",
            server=MagicMock(client=client),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as http:
            return await http.post("/api/settings/restart")

    async def test_restart_reloads_the_server_entry_and_closes_its_probe(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        embedded: list[str],
    ) -> None:
        entry = add_server_entry(hass)

        resp = await self._restart(component_core_client)

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert embedded == [entry.entry_id]
        assert (
            hass.config_entries.options.async_progress_by_handler(entry.entry_id) == []
        )

    async def test_no_server_entry_is_a_409(
        self, component_core_client: HomeAssistantClient, embedded: list[str]
    ) -> None:
        """Below 500 on purpose: the page's restart JS reads a 5xx as a restart
        in flight and starts reloading."""
        resp = await self._restart(component_core_client)

        assert resp.status_code == 409
        assert embedded == []

    async def test_unreachable_home_assistant_is_not_reported_as_no_entry(
        self, unreachable_client: HomeAssistantClient, embedded: list[str]
    ) -> None:
        """ "Entry not found" steers the user to reinstall a running component."""
        resp = await self._restart(unreachable_client)

        assert resp.status_code == 409
        error = resp.json()["error"]
        assert error["code"] == "CONNECTION_FAILED"
        assert "not locate" not in error["message"]
        assert embedded == []

    def test_embedded_mode_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        app = _app("settings_info", "/api/settings/info", "GET", server=MagicMock())

        resp = TestClient(app).get("/api/settings/info")

        assert resp.json()["deployment_mode"] == "embedded"
