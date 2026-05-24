"""Unit tests for the settings UI config persistence and tool visibility."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from ha_mcp.settings_ui import (
    FEATURE_GATED_TOOLS,
    MANDATORY_TOOLS,
    TRANSFORM_GENERATED_TOOLS,
    _get_config_path,
    _get_tool_metadata,
    apply_tool_visibility,
    load_tool_config,
    register_settings_routes,
    save_tool_config,
)

SaveHandler = Callable[[Request], Awaitable[JSONResponse]]


async def _drain_background_restart_tasks() -> None:
    """Deterministically wait for every in-flight self-restart task.

    `_schedule_supervisor_self_restart` is fire-and-forget but keeps
    strong references in `_BACKGROUND_RESTART_TASKS` (so the GC doesn't
    reap mid-run). Tests that exercise the schedule helper need to
    wait for those tasks to finish before asserting on side effects;
    `asyncio.sleep`-based polling is flaky on slow CI runners, so
    snapshot the set and await each task.
    """
    import asyncio

    from ha_mcp.settings_ui import _BACKGROUND_RESTART_TASKS

    pending = list(_BACKGROUND_RESTART_TASKS)
    if pending:
        await asyncio.wait(pending)


class TestConfigPersistence:
    """Test load/save of tool_config.json."""

    def test_save_and_load(self, tmp_path: Path):
        config = {"tools": {"ha_hacs_info": "disabled", "ha_restart": "pinned"}}
        config_path = tmp_path / "tool_config.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            save_tool_config(config)
            loaded = load_tool_config()
        assert loaded == config

    def test_load_missing_file(self, tmp_path: Path):
        config_path = tmp_path / "nonexistent.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            assert load_tool_config() == {}

    def test_load_corrupt_file(self, tmp_path: Path):
        config_path = tmp_path / "corrupt.json"
        config_path.write_text("not json {{{")
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            assert load_tool_config() == {}

    def test_seed_from_env_vars(self, tmp_path: Path):
        config_path = tmp_path / "tool_config.json"
        settings = MagicMock()
        settings.disabled_tools = "ha_hacs_info,ha_hacs_download"
        settings.pinned_tools = "ha_restart"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            config = load_tool_config(settings)
        assert config["tools"]["ha_hacs_info"] == "disabled"
        assert config["tools"]["ha_hacs_download"] == "disabled"
        assert config["tools"]["ha_restart"] == "pinned"
        assert config_path.exists()


class TestApplyToolVisibility:
    """Test apply_tool_visibility logic."""

    def test_disables_tools(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_hacs_info": "disabled", "ha_restart": "enabled"}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_hacs_info" in disabled_names
        assert "ha_restart" not in disabled_names

    def test_mandatory_tools_not_disabled(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": dict.fromkeys(MANDATORY_TOOLS, "disabled")}
        apply_tool_visibility(mcp, config, settings)
        if mcp.disable.called:
            disabled_names = mcp.disable.call_args[1]["names"]
            for name in MANDATORY_TOOLS:
                assert name not in disabled_names

    def test_yaml_editing_off_disables_tool(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = False
        config = {"tools": {}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_config_set_yaml" in disabled_names

    def test_yaml_editing_on_does_not_disable_tool(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {}}
        apply_tool_visibility(mcp, config, settings)
        if mcp.disable.called:
            disabled_names = mcp.disable.call_args[1]["names"]
            assert "ha_config_set_yaml" not in disabled_names

    def test_yaml_editing_on_but_ui_disabled_keeps_tool_disabled(self):
        # AND semantics: even when the safety toggle is on, a UI-saved
        # "disabled" state must be respected. (Regression guard for
        # Patch76 G9.2 — the previous behavior force-enabled the tool
        # whenever the safety toggle was on, overriding the UI choice.)
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_config_set_yaml": "disabled"}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_config_set_yaml" in disabled_names

    def test_returns_pinned_names(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_restart": "pinned", "ha_hacs_info": "enabled"}}
        result = apply_tool_visibility(mcp, config, settings)
        assert "ha_restart" in result.pinned_names
        assert "ha_hacs_info" not in result.pinned_names

    def test_returns_explicitly_enabled_names(self):
        """Tools toggled to ``"enabled"`` are surfaced so the server can
        unpin them from DEFAULT_PINNED_TOOLS (#966)."""
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {
            "tools": {
                "ha_manage_backup": "enabled",  # would otherwise be a default pin
                "ha_restart": "pinned",
                "ha_hacs_info": "disabled",
            }
        }
        result = apply_tool_visibility(mcp, config, settings)
        assert "ha_manage_backup" in result.enabled_names
        assert "ha_restart" not in result.enabled_names
        assert "ha_hacs_info" not in result.enabled_names

    def test_empty_config_no_disable(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_not_called()

    def test_default_pinned_tool_can_be_unpinned_via_enabled_state(self):
        """End-to-end coverage for the #966 unpin-default behavior.

        Mirrors the server-side filter in
        ``HomeAssistantSmartMCPServer._apply_tool_search``: the effective
        ``always_visible`` set is ``DEFAULT_PINNED_TOOLS`` minus any tool
        the user explicitly toggled to ``"enabled"``. A tool's presence
        in ``DEFAULT_PINNED_TOOLS`` is a default, not a floor — users get
        the final say via the Tools tab.
        """
        from ha_mcp.transforms import DEFAULT_PINNED_TOOLS

        # Pick a default-pinned tool that's NOT in MANDATORY_TOOLS so the
        # config-side disable path can't interfere with the assertion.
        # ha_config_get_automation is in DEFAULT_PINNED_TOOLS but not in
        # MANDATORY_TOOLS.
        assert "ha_config_get_automation" in DEFAULT_PINNED_TOOLS
        assert "ha_config_get_automation" not in MANDATORY_TOOLS

        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_config_get_automation": "enabled"}}
        result = apply_tool_visibility(mcp, config, settings)

        # Server.py's filter: pinned = [n for n in DEFAULT_PINNED_TOOLS
        #                              if n not in result.enabled_names]
        effective_pinned = [
            name for name in DEFAULT_PINNED_TOOLS if name not in result.enabled_names
        ]
        assert "ha_config_get_automation" not in effective_pinned
        # Tools NOT in the config keep their default pinning.
        assert "ha_manage_backup" in effective_pinned


@pytest.fixture(autouse=True)
def _reset_data_dir_cache():
    """Clear the shared resolved-dir cache between tests."""
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


class TestConfigPath:
    """Thin wrapper around utils.data_paths.get_data_dir; full priority
    order is tested in tests/src/unit/test_data_paths.py.
    """

    def test_returns_data_dir_plus_filename(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _get_config_path() == tmp_path / ".ha-mcp" / "tool_config.json"

    def test_load_tool_config_does_not_crash_on_unreadable_config_dir(
        self, monkeypatch, tmp_path
    ):
        """Regression for #1125 + the same-class follow-up bug.

        When the resolved path's parent isn't traversable by the runtime
        UID (e.g. ``HA_MCP_CONFIG_DIR`` pointing at an existing 0700 dir
        owned by another user), ``Path.exists()`` would raise
        ``PermissionError`` because ``EACCES`` is not in
        ``pathlib._IGNORED_ERRNOS``. ``load_tool_config()`` must treat it
        as "no config yet" instead of crashing.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        unreadable_dir = tmp_path / "unreadable"
        unreadable_dir.mkdir()
        cfg_path = unreadable_dir / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: cfg_path)

        original_read = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs):
            if self == cfg_path:
                raise PermissionError(13, "Permission denied")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        # Must not raise.
        assert load_tool_config() == {}

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="chmod 0o000 doesn't model POSIX EACCES on Windows",
    )
    def test_load_tool_config_handles_real_eacces_on_posix(self, monkeypatch, tmp_path):
        """End-to-end variant of the EACCES regression: a real 0o000 dir.

        The mocked-``read_text`` test above pins the going-forward contract,
        but a future maintainer who reintroduces an upstream ``Path.exists()``
        check would not be caught by it. This test exercises the actual
        permission boundary: ``read_text`` on a file under a 0o000 dir
        raises ``PermissionError`` (errno EACCES) from the kernel.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        cfg_path = locked_dir / "tool_config.json"
        cfg_path.write_text("{}")
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: cfg_path)
        os.chmod(locked_dir, 0o000)
        try:
            assert load_tool_config() == {}
        finally:
            os.chmod(locked_dir, 0o755)  # let pytest clean up tmp_path


class TestSaveToolConfig:
    """Tests for the bool return contract added so the HTTP route can
    surface failures to the UI instead of lying that the save succeeded."""

    def test_returns_true_on_success(self, tmp_path):
        cfg_path = tmp_path / "tool_config.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=cfg_path):
            assert save_tool_config({"tools": {"x": "disabled"}}) is True
        assert cfg_path.exists()

    def test_returns_false_on_oserror(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: cfg_path)

        # ``save_tool_config`` now writes via ``_atomic_write_json``
        # (tmp + ``os.replace``) so a read-only filesystem can surface
        # at either step — patch the helper itself so the simulation
        # doesn't need to know which underlying call raises. The old
        # patch-Path.write_text approach also recursed once we wrote
        # to ``<target>.tmp`` (the fallback ``Path.write_text(self,...)``
        # call points back at the now-monkeypatched function).
        def fake_atomic_write(path: Path, payload: dict) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr("ha_mcp.settings_ui._atomic_write_json", fake_atomic_write)
        assert save_tool_config({"tools": {"x": "disabled"}}) is False


class TestTransformGeneratedTools:
    """``TRANSFORM_GENERATED_TOOLS`` is the injection point for runtime-
    appended transform tools. No transforms currently append tools that
    need settings-UI visibility (#1134 consolidated the prior pair into
    the normally-registered ``ha_get_skill_guide``), so the dict is
    empty. Keeping the type/contract intact so future transform-appended
    tools have a place to land without re-introducing the dispatch path.
    """

    def test_dict_exists_and_is_empty(self):
        assert TRANSFORM_GENERATED_TOOLS == {}

    @pytest.mark.asyncio
    async def test_metadata_omits_pre_consolidation_tools(self):
        """With no transform stubs, _get_tool_metadata must not surface
        the pre-#1134 ha_list_resources / ha_read_resource pair. Feature-
        gated stubs are still injected by a separate path (covered in
        TestFeatureGatedTools) so the result isn't empty.
        """
        server = MagicMock()
        server.mcp.local_provider._list_tools = AsyncMock(return_value=[])

        tools = await _get_tool_metadata(server)
        names = {t["name"] for t in tools}

        assert "ha_list_resources" not in names
        assert "ha_read_resource" not in names


class TestFeatureGatedTools:
    """Test the FEATURE_GATED_TOOLS dict aligns with the beta tag system."""

    def test_install_mcp_tools_is_gated(self):
        # Patch76 G7: ha_install_mcp_tools must appear as a stub when its
        # feature flag is off; otherwise users have no way to discover the
        # tool exists.
        assert "ha_install_mcp_tools" in FEATURE_GATED_TOOLS
        assert FEATURE_GATED_TOOLS["ha_install_mcp_tools"]["disabled_by"] == (
            "enable_custom_component_integration"
        )

    def test_filesystem_tools_use_addon_option_name(self):
        # disabled_by should reference the dev addon option name (matches
        # how the JS renders "set <code>{disabled_by}</code> in the dev
        # add-on config or the matching env var (see docs/beta.md)").
        for name in (
            "ha_list_files",
            "ha_read_file",
            "ha_write_file",
            "ha_delete_file",
        ):
            assert FEATURE_GATED_TOOLS[name]["disabled_by"] == "enable_filesystem_tools"


class TestRouteRegistration:
    """Test register_settings_routes mounting under secret_path (Patch76 G1)."""

    def _collect_paths(self, mcp):
        return [call.args[0] for call in mcp.custom_route.call_args_list]

    def test_registers_root_in_addon_mode(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="/private_x")
        paths = self._collect_paths(mcp)
        # Root for ingress + secret-prefixed for direct port access
        assert "/" in paths
        assert "/settings" in paths
        assert "/private_x/settings" in paths
        assert "/private_x/api/settings/tools" in paths

    def test_secret_path_only_when_not_addon(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="/mcp")
        paths = self._collect_paths(mcp)
        # No root mount in Docker/standalone — only the secret-prefixed routes
        assert "/" not in paths
        assert "/settings" not in paths
        assert "/mcp/settings" in paths
        assert "/mcp/api/settings/tools" in paths

    def test_no_routes_when_no_addon_and_no_secret(self, monkeypatch):
        # Refuse to mount publicly: no auth → no routes.
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="")
        assert mcp.custom_route.call_count == 0


class TestSaveToolsValidation:
    """Test POST /api/settings/tools handler validation (Patch76 G3)."""

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_handler(self, monkeypatch) -> SaveHandler:
        # Capture the _save_tools handler that register_settings_routes
        # mounts so we can call it directly instead of going through HTTP.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, Any] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path == "/api/settings/tools" and "POST" in methods:
                    captured["save"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        register_settings_routes(mcp, MagicMock(), secret_path="/x")
        return captured["save"]

    @pytest.mark.asyncio
    async def test_rejects_non_dict_body_array(self, monkeypatch, tmp_path):
        # Patch76 G3: a JSON array body would AttributeError on body.get
        # → 500. Must be a structured 400 instead.
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request([1, 2, 3]))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["success"] is False

    @pytest.mark.asyncio
    async def test_rejects_non_dict_body_null(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request(None))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_non_dict_states(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request({"states": "not-a-dict"}))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_drops_garbage_state_values(self, monkeypatch, tmp_path):
        config_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: config_path)
        save = self._capture_handler(monkeypatch)
        resp = await save(
            self._make_request(
                {
                    "states": {
                        "ha_good_tool": "disabled",
                        "ha_bad_value": "not_a_real_state",
                        42: "disabled",  # non-string key
                    },
                }
            )
        )
        assert resp.status_code == 200
        saved = json.loads(config_path.read_text())
        assert saved["tools"] == {"ha_good_tool": "disabled"}

    @pytest.mark.asyncio
    async def test_returns_500_when_save_fails(self, monkeypatch, tmp_path):
        """``save_tool_config`` returning False (read-only fs, etc.) must
        surface as a 500 to the UI — otherwise the JS shows "Saved" while
        the change was lost."""
        config_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: config_path)
        monkeypatch.setattr("ha_mcp.settings_ui.save_tool_config", lambda _: False)
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request({"states": {"ha_good_tool": "disabled"}}))
        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["success"] is False
        assert "HA_MCP_CONFIG_DIR" in str(body)


class TestRestartAddon:
    """Tests for the `/api/settings/restart` handler — pins the previously
    untested branches in `_restart_addon`. Boy-Scout pin landed alongside
    the `verify_ssl` propagation in this PR. Symbol-based references below
    rather than line numbers, since the kwarg-split here shifts them."""

    def _capture_handler(self, monkeypatch, *, with_token: bool = True) -> SaveHandler:
        """Capture the `_restart_addon` closure from `register_settings_routes`.

        Mirrors `TestSaveToolsValidation._capture_handler`. `with_token`
        toggles the env so the no-token branch and the happy-path branches
        can both be exercised from the same fixture.
        """
        if with_token:
            monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
        else:
            monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

        captured: dict[str, Any] = {}

        def custom_route_factory(path: str, methods: list[str]):
            def decorator(fn: Any) -> Any:
                if path.endswith("/api/settings/restart") and "POST" in methods:
                    captured["restart"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        # `_restart_addon` reads `server.settings.verify_ssl` — must resolve
        # to a real bool, not a MagicMock, because httpx accepts only
        # bool/SSLContext for `verify=`.
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured["restart"]

    def _make_request(self, *, body: Any = None) -> MagicMock:
        """Build a request mock whose ``.json()`` returns ``body``.

        ``body=None`` simulates an empty/missing body — the JSONDecodeError
        path inside ``_restart_addon`` — so the slug defaults to "self".
        Pass a dict to simulate a JSON-bodied POST (the inaddon E2E uses
        ``{"slug": "<addon>"}`` to target a non-self addon).
        """
        request = MagicMock()
        if body is None:
            request.json = AsyncMock(side_effect=json.JSONDecodeError("empty", "", 0))
        else:
            request.json = AsyncMock(return_value=body)
        return request

    def _patch_supervisor_client(
        self, *, post_side_effect=None, post_return=None
    ) -> tuple[Any, Any]:
        """Patch ``make_supervisor_httpx_client`` and return ``(patcher, mock_client)``.

        The factory's own contract (base_url, Authorization header) is
        pinned by ``test_supervisor_client.py``; these tests only check
        what URL ``_restart_addon`` posts to and how it handles responses.
        """
        mock_client = MagicMock()
        if post_side_effect is not None:
            mock_client.post = AsyncMock(side_effect=post_side_effect)
        else:
            mock_client.post = AsyncMock(return_value=post_return)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=cm)
        patcher = patch("ha_mcp.settings_ui.make_supervisor_httpx_client", factory)
        return patcher, mock_client

    @pytest.mark.asyncio
    async def test_returns_400_without_supervisor_token(self, monkeypatch):
        """No-token branch (the `if not token:` guard at the top of
        `_restart_addon`): when SUPERVISOR_TOKEN is unset (non-addon
        install), the endpoint must surface a structured 400 rather than
        ever reaching the Supervisor URL.
        """
        restart = self._capture_handler(monkeypatch, with_token=False)
        request = self._make_request()

        resp = await restart(request)

        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_FAILED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_cls",
        [httpx.ReadError, httpx.RemoteProtocolError],
    )
    async def test_treats_connection_drop_as_success(self, monkeypatch, exc_cls):
        """Drop-as-success branch (the catch on
        `(ReadError, RemoteProtocolError)` inside the `httpx.AsyncClient`
        block): when restarting a *sibling* addon, the Supervisor kills
        that target mid-request, so the connection-drop is the documented
        success signal — not a failure to surface. ConnectError is
        excluded because it fires BEFORE a connection is established (DNS
        / TCP refused / socket misconfigured) and means Supervisor was
        unreachable, not that a restart was initiated.

        Self-restart no longer flows through this code path — see
        ``test_self_restart_returns_200_and_schedules_background_task``
        — so the synchronous error-surfacing only fires for non-self
        slugs. Mirror that asymmetry here by targeting a non-self addon.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body={"slug": "core_ssh"})

        patcher, _ = self._patch_supervisor_client(post_side_effect=exc_cls("kill"))
        with patcher:
            resp = await restart(request)

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert "Restart initiated" in body["message"]

    @pytest.mark.asyncio
    async def test_connect_error_returns_502(self, monkeypatch):
        """ConnectError fires before a connection is established and means
        Supervisor was unreachable — must NOT be treated as a successful
        restart. Falls through to the generic `httpx.HTTPError` handler
        which returns 502 with `CONNECTION_FAILED`. Self-restart no
        longer touches this path; use a non-self slug to exercise it.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body={"slug": "core_ssh"})

        patcher, _ = self._patch_supervisor_client(
            post_side_effect=httpx.ConnectError("no route")
        )
        with patcher:
            resp = await restart(request)

        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_generic_http_error_returns_502(self, monkeypatch):
        """The generic `httpx.HTTPError` handler (catches anything not
        already special-cased) maps to 502 + CONNECTION_FAILED. Pins the
        last uncovered transport-error path in `_restart_addon` —
        non-self slug exercises the synchronous branch.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body={"slug": "core_ssh"})

        # PoolTimeout subclasses httpx.HTTPError but is NOT in the
        # drop-as-success tuple — exercises the fall-through.
        patcher, _ = self._patch_supervisor_client(
            post_side_effect=httpx.PoolTimeout("pool full")
        )
        with patcher:
            resp = await restart(request)

        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_supervisor_4xx_returns_502(self, monkeypatch):
        """When Supervisor returns a non-2xx status (e.g. 401 Unauthorized)
        for a *sibling* addon restart, the handler must surface a 502 to
        the caller — the restart was not initiated. Pins the
        `status_code >= 400` branch in `_restart_addon`. Self-restart
        path no longer surfaces supervisor errors (see
        ``_schedule_supervisor_self_restart``); use a non-self slug.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body={"slug": "core_ssh"})

        response = MagicMock()
        response.status_code = 401
        response.text = "Unauthorized"
        patcher, _ = self._patch_supervisor_client(post_return=response)
        with patcher:
            resp = await restart(request)

        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["success"] is False

    @pytest.mark.asyncio
    async def test_self_restart_returns_200_and_schedules_background_task(
        self, monkeypatch
    ):
        """No-body request → target_slug='self' → schedules a background
        ``/addons/self/restart`` POST and returns 200 immediately so the
        JSON response can flush through HA ingress *before* Supervisor
        kills the addon mid-response. Without this, ingress converts the
        dropped upstream into a 5xx Bad Gateway, which the JS rendered
        as 'Restart failed' even when the restart actually succeeded.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request()

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart",
            schedule_mock,
        )
        resp = await restart(request)

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert "Restart initiated" in body["message"]
        schedule_mock.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_slug_in_body_targets_named_addon(self, monkeypatch):
        """Body ``{"slug": "<other>"}`` → POSTs to ``/addons/<other>/restart``.

        Lets the inaddon E2E suite exercise the real Supervisor restart
        wire contract against a non-test-critical addon without taking
        the dev addon (and the running ``mcp_client``) down. The historical
        body-less behavior (slug defaults to "self") is pinned by
        ``test_posts_relative_url_with_ctor_authorization``.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body={"slug": "core_ssh"})

        response = MagicMock()
        response.status_code = 200
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ha_mcp.settings_ui.httpx.AsyncClient", return_value=cm):
            resp = await restart(request)

        assert resp.status_code == 200
        mock_client.post.assert_awaited_once_with("/addons/core_ssh/restart")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},  # no slug key
            {"slug": ""},  # empty string
            {"slug": "   "},  # whitespace only
            {"slug": 42},  # non-string
            {"slug": None},  # explicit None
            "not-a-dict",  # body is a string, not a dict
            # Path-traversal / injection probes — the whitelist must reject
            # all of these, falling back to "self" rather than building
            # ``/addons/<malicious>/restart``. Even though Supervisor would
            # reject most, validating at the edge is cheaper than relying
            # on downstream rejection (Gemini PR review flagged path
            # traversal as a security-high concern).
            {"slug": "../evil"},
            {"slug": "self/../something"},
            {"slug": "a/b"},
            {"slug": "addon;rm -rf"},
            {"slug": "%2e%2e%2fself"},
            {"slug": "self?action=delete"},
            {"slug": "self#frag"},
        ],
    )
    async def test_invalid_slug_in_body_falls_back_to_self(self, monkeypatch, body):
        """Malformed/missing ``slug`` field → restart targets ``self``.

        Preserves the historical self-restart behavior when callers post
        a body that doesn't carry a usable slug. The settings-UI restart
        button posts no body at all; the explicit slug paths exist purely
        for the E2E test surface and should never accidentally redirect
        a self-restart to ``/addons//restart`` or similar.

        Self-restart now flows through ``_schedule_supervisor_self_restart``
        (background task pattern — see
        ``test_self_restart_returns_200_and_schedules_background_task``),
        so the verifier here is "the schedule helper got called" rather
        than "Supervisor was POSTed synchronously". Patch the supervisor
        client factory at its public API to confirm the synchronous path
        is NOT invoked for the fall-back-to-self case.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body=body)

        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart",
            schedule_mock,
        )

        # Patch the same surface the schedule-vs-sync branch consults:
        # ``make_supervisor_httpx_client`` (not the lower-level
        # ``httpx.AsyncClient``). Lets us assert the synchronous POST
        # was never awaited without juggling the AsyncClient context-
        # manager dance inline.
        response = MagicMock()
        response.status_code = 200
        patcher, mock_client = self._patch_supervisor_client(post_return=response)
        with patcher:
            await restart(request)

        schedule_mock.assert_called_once_with(True)
        mock_client.post.assert_not_awaited()


class TestBackupSettingsOverridePersistence:
    """Round-trip tests for the auto-backup override file (#1288 web UI editor)."""

    def test_save_and_load_roundtrip(self, monkeypatch, tmp_path):
        from ha_mcp.settings_ui import (
            _load_backup_settings_override,
            _save_backup_settings_override,
        )

        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "backup_settings.json",
        )
        payload = {
            "enable_auto_backup": True,
            "auto_backup_throttle_minutes": 5,
            "auto_backup_retain_per_entity": 50,
        }
        assert _save_backup_settings_override(payload) is True
        assert _load_backup_settings_override() == payload

    def test_load_missing_returns_empty(self, monkeypatch, tmp_path):
        from ha_mcp.settings_ui import _load_backup_settings_override

        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "absent.json",
        )
        assert _load_backup_settings_override() == {}

    def test_load_corrupt_returns_empty(self, monkeypatch, tmp_path):
        from ha_mcp.settings_ui import _load_backup_settings_override

        path = tmp_path / "backup_settings.json"
        path.write_text("not valid json {{{")
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: path,
        )
        assert _load_backup_settings_override() == {}

    def test_load_non_dict_returns_empty(self, monkeypatch, tmp_path):
        from ha_mcp.settings_ui import _load_backup_settings_override

        path = tmp_path / "backup_settings.json"
        path.write_text("[1, 2, 3]")
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: path,
        )
        assert _load_backup_settings_override() == {}


class TestGetBackupSettingOrigin:
    """Origin detection for the per-field editable matrix.

    The Web UI relies on this to label each field and disable inputs
    when the value comes from an env var the user explicitly set.
    """

    def test_addon_token_wins(self, monkeypatch):
        from ha_mcp.config import get_backup_setting_origin

        monkeypatch.setenv("SUPERVISOR_TOKEN", "abc")
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
        # Even with env var set, addon-mode reports "addon" because the
        # value source-of-truth is config.yaml via start.py.
        assert get_backup_setting_origin("ENABLE_AUTO_BACKUP") == "addon"

    def test_env_var_set_returns_env(self, monkeypatch):
        from ha_mcp.config import get_backup_setting_origin

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "15")
        assert get_backup_setting_origin("AUTO_BACKUP_THROTTLE_MINUTES") == "env"

    def test_file_present_returns_file(self, monkeypatch, tmp_path):
        import ha_mcp.config as cfg_mod

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("ENABLE_AUTO_BACKUP", raising=False)
        override = {"enable_auto_backup": True}
        (tmp_path / "backup_settings.json").write_text(json.dumps(override))
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        assert cfg_mod.get_backup_setting_origin("ENABLE_AUTO_BACKUP") == "file"

    def test_no_env_no_file_returns_default(self, monkeypatch, tmp_path):
        from ha_mcp.config import get_backup_setting_origin

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("AUTO_BACKUP_RETAIN_PER_ENTITY", raising=False)
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        assert get_backup_setting_origin("AUTO_BACKUP_RETAIN_PER_ENTITY") == "default"

    def test_unknown_env_var_returns_default(self, monkeypatch, tmp_path):
        from ha_mcp.config import get_backup_setting_origin

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        # Env var not in BACKUP_OVERRIDE_FIELDS — origin lookup still safe.
        assert get_backup_setting_origin("NOT_A_REAL_ENV_VAR") == "default"


class TestApplyBackupOverrides:
    """``get_global_settings`` applies the override file unless env wins."""

    def test_file_value_applied_when_no_env(self, monkeypatch, tmp_path):
        import ha_mcp.config as cfg_mod

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        for env in (
            "ENABLE_AUTO_BACKUP",
            "AUTO_BACKUP_THROTTLE_MINUTES",
            "AUTO_BACKUP_RETAIN_PER_ENTITY",
        ):
            monkeypatch.delenv(env, raising=False)
        override = {
            "enable_auto_backup": True,
            "auto_backup_throttle_minutes": 7,
            "auto_backup_retain_per_entity": 33,
        }
        (tmp_path / "backup_settings.json").write_text(json.dumps(override))
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        cfg_mod._reset_global_settings()
        s = cfg_mod.get_global_settings()
        assert s.enable_auto_backup is True
        assert s.auto_backup_throttle_minutes == 7
        assert s.auto_backup_retain_per_entity == 33
        cfg_mod._reset_global_settings()

    def test_env_var_wins_over_file(self, monkeypatch, tmp_path):
        import ha_mcp.config as cfg_mod

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "false")
        monkeypatch.delenv("AUTO_BACKUP_THROTTLE_MINUTES", raising=False)
        override = {
            "enable_auto_backup": True,  # env var below sets to false
            "auto_backup_throttle_minutes": 42,  # no env var → file wins
        }
        (tmp_path / "backup_settings.json").write_text(json.dumps(override))
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        cfg_mod._reset_global_settings()
        s = cfg_mod.get_global_settings()
        assert s.enable_auto_backup is False  # env wins
        assert s.auto_backup_throttle_minutes == 42  # file applied
        cfg_mod._reset_global_settings()

    def test_addon_mode_ignores_override_file(self, monkeypatch, tmp_path):
        import ha_mcp.config as cfg_mod

        monkeypatch.setenv("SUPERVISOR_TOKEN", "abc")
        # start.py would set this in real addon; simulate.
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "false")
        # Override file says True — must be ignored in addon mode.
        (tmp_path / "backup_settings.json").write_text(
            json.dumps({"enable_auto_backup": True})
        )
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        cfg_mod._reset_global_settings()
        s = cfg_mod.get_global_settings()
        assert s.enable_auto_backup is False
        cfg_mod._reset_global_settings()

    def test_out_of_range_skipped(self, monkeypatch, tmp_path):
        import ha_mcp.config as cfg_mod

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        for env in (
            "AUTO_BACKUP_THROTTLE_MINUTES",
            "AUTO_BACKUP_RETAIN_PER_ENTITY",
        ):
            monkeypatch.delenv(env, raising=False)
        # Both above their bounds — must be silently skipped, defaults survive.
        (tmp_path / "backup_settings.json").write_text(
            json.dumps(
                {
                    "auto_backup_throttle_minutes": 9999,
                    "auto_backup_retain_per_entity": 999_999,
                }
            )
        )
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        cfg_mod._reset_global_settings()
        s = cfg_mod.get_global_settings()
        assert s.auto_backup_throttle_minutes == 0  # default
        assert s.auto_backup_retain_per_entity == 100  # default
        cfg_mod._reset_global_settings()


class TestSaveBackupConfigEndpoint:
    """POST /api/settings/backup-config validation + env-pin rejection."""

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_handlers(
        self, monkeypatch, *, addon: bool = False
    ) -> dict[str, SaveHandler]:
        if addon:
            monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        else:
            monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/backup-config"):
                    if "GET" in methods:
                        captured["get"] = fn
                    if "POST" in methods:
                        captured["post"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured

    @pytest.mark.asyncio
    async def test_rejects_non_object_body(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "backup_settings.json",
        )
        handlers = self._capture_handlers(monkeypatch)
        resp = await handlers["post"](self._make_request([1, 2, 3]))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_throttle(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "backup_settings.json",
        )
        handlers = self._capture_handlers(monkeypatch)
        resp = await handlers["post"](
            self._make_request({"auto_backup_throttle_minutes": 9999})
        )
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "0..1440" in str(body)

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_retain(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "backup_settings.json",
        )
        handlers = self._capture_handlers(monkeypatch)
        resp = await handlers["post"](
            self._make_request({"auto_backup_retain_per_entity": 0})
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_unknown_only_body(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "backup_settings.json",
        )
        handlers = self._capture_handlers(monkeypatch)
        resp = await handlers["post"](self._make_request({"unrelated_key": True}))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_env_pinned_field_returns_409(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: tmp_path / "backup_settings.json",
        )
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
        handlers = self._capture_handlers(monkeypatch)
        resp = await handlers["post"](self._make_request({"enable_auto_backup": False}))
        assert resp.status_code == 409
        body = json.loads(resp.body)
        assert body["success"] is False
        assert any(
            r["env_var"] == "ENABLE_AUTO_BACKUP" for r in body["error"]["rejected"]
        )

    @pytest.mark.asyncio
    async def test_standalone_writes_file_and_invalidates_cache(
        self, monkeypatch, tmp_path
    ):
        import ha_mcp.config as cfg_mod
        import ha_mcp.settings_ui as sui_mod

        override_path = tmp_path / "backup_settings.json"
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_backup_settings_override_path",
            lambda: override_path,
        )
        # Critical: the get_data_dir patch is what the *config* module reads
        # via _read_backup_override_file when get_global_settings re-reads
        # after the cache reset. Without it the override file wouldn't be
        # found on the post-reset read, so cache invalidation appears to
        # have no effect even though the POST succeeded.
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        for env in (
            "ENABLE_AUTO_BACKUP",
            "AUTO_BACKUP_THROTTLE_MINUTES",
            "AUTO_BACKUP_RETAIN_PER_ENTITY",
        ):
            monkeypatch.delenv(env, raising=False)
        handlers = self._capture_handlers(monkeypatch)
        cfg_mod._reset_global_settings()
        _ = cfg_mod.get_global_settings()  # warm the cache
        resp = await handlers["post"](
            self._make_request(
                {
                    "enable_auto_backup": True,
                    "auto_backup_throttle_minutes": 9,
                }
            )
        )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["mode"] == "file"
        # File-mode auto-backup save applies live via the cache reset;
        # no addon restart needed, hence restart_required=False. The
        # field name was renamed from "restarting" → "restart_required"
        # as part of the unified restart flow (Tools / Server Settings
        # / Backups all use the same field).
        assert body["restart_required"] is False
        assert "restarting" not in body  # legacy field name must be gone
        on_disk = json.loads(override_path.read_text())
        assert on_disk["enable_auto_backup"] is True
        assert on_disk["auto_backup_throttle_minutes"] == 9
        # Cache invalidation publishes the new values to the next read.
        fresh = cfg_mod.get_global_settings()
        assert fresh.enable_auto_backup is True
        assert fresh.auto_backup_throttle_minutes == 9
        cfg_mod._reset_global_settings()
        # Guarantee no symbol-import lint trip.
        assert sui_mod is not None


class TestSupervisorOptionsHelpers:
    """Module-level helpers for the addon-mode Supervisor options flow.

    Supervisor's ``/addons/<slug>/options`` POST is a *full* replacement
    validated against the addon schema, so a partial body (e.g. only the
    auto-backup fields the user changed) is rejected with
    ``addon_configuration_invalid_error`` when a required key like
    ``backup_hint`` is omitted. These tests pin the merge contract that
    ``_save_backup_config`` and ``_save_feature_flags`` now rely on in
    addon mode.
    """

    def _patch_supervisor_client(
        self,
        *,
        get_response=None,
        post_response=None,
        get_side_effect=None,
        post_side_effect=None,
    ):
        """Return (patcher, mock_client) where mock_client.get/post are AsyncMocks."""
        mock_client = MagicMock()
        if get_side_effect is not None:
            mock_client.get = AsyncMock(side_effect=get_side_effect)
        else:
            mock_client.get = AsyncMock(return_value=get_response)
        if post_side_effect is not None:
            mock_client.post = AsyncMock(side_effect=post_side_effect)
        else:
            mock_client.post = AsyncMock(return_value=post_response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=cm)
        patcher = patch("ha_mcp.settings_ui.make_supervisor_httpx_client", factory)
        return patcher, mock_client

    @pytest.mark.asyncio
    async def test_fetch_current_options_unwraps_data_envelope(self, monkeypatch):
        """Supervisor wraps responses in ``{"result": "ok", "data": {...}}``.
        The fetch helper must unwrap the envelope and return the inner
        options dict.
        """
        from ha_mcp.settings_ui import _supervisor_fetch_current_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        response = MagicMock()
        response.status_code = 200
        response.json = MagicMock(
            return_value={
                "result": "ok",
                "data": {
                    "options": {
                        "backup_hint": "normal",
                        "enable_tool_search": True,
                    }
                },
            }
        )
        patcher, _ = self._patch_supervisor_client(get_response=response)
        with patcher:
            options, err = await _supervisor_fetch_current_options(verify_ssl=True)
        assert err is None
        assert options == {"backup_hint": "normal", "enable_tool_search": True}

    @pytest.mark.asyncio
    async def test_fetch_current_options_accepts_bare_options_dict(self, monkeypatch):
        """``_supervisor_fetch_current_options`` documents that it accepts
        BOTH the wrapped ``{"data": {"options": ...}}`` envelope AND a
        bare ``{"options": ...}`` shape (older mocks / variants). Pin the
        bare-dict path so a future supervisor variant or mock cleanup
        cannot silently regress it.
        """
        from ha_mcp.settings_ui import _supervisor_fetch_current_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        response = MagicMock()
        response.status_code = 200
        response.json = MagicMock(
            return_value={"options": {"backup_hint": "normal", "verify_ssl": True}}
        )
        patcher, _ = self._patch_supervisor_client(get_response=response)
        with patcher:
            options, err = await _supervisor_fetch_current_options(verify_ssl=True)
        assert err is None
        assert options == {"backup_hint": "normal", "verify_ssl": True}

    @pytest.mark.asyncio
    async def test_fetch_current_options_supervisor_4xx_returns_transport_error(
        self, monkeypatch
    ):
        """Supervisor 4xx/5xx on the /info GET is a transport-class failure
        (we sent no body — there is no schema for the GET to validate).
        Must be classified ``kind="transport"`` with status_code=502 so
        the route maps it to CONNECTION_FAILED rather than the
        schema-recovery suggestions of CONFIG_VALIDATION_FAILED.
        """
        from ha_mcp.settings_ui import _supervisor_fetch_current_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        response = MagicMock()
        response.status_code = 503
        response.text = "Supervisor busy"
        patcher, _ = self._patch_supervisor_client(get_response=response)
        with patcher:
            options, err = await _supervisor_fetch_current_options(verify_ssl=True)
        assert options == {}
        assert err is not None
        assert err.kind == "transport"
        assert err.status_code == 502
        assert "503" in err.message

    @pytest.mark.asyncio
    async def test_fetch_current_options_http_error_returns_transport_error(
        self, monkeypatch
    ):
        from ha_mcp.settings_ui import _supervisor_fetch_current_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        patcher, _ = self._patch_supervisor_client(
            get_side_effect=httpx.ConnectError("no route")
        )
        with patcher:
            options, err = await _supervisor_fetch_current_options(verify_ssl=True)
        assert options == {}
        assert err is not None
        assert err.kind == "transport"
        assert err.status_code == 502

    @pytest.mark.asyncio
    async def test_fetch_current_options_runtime_error_returns_transport_error(
        self, monkeypatch
    ):
        """``make_supervisor_httpx_client`` raises RuntimeError when
        SUPERVISOR_TOKEN is unset. Both call sites gate on the env var
        first, but the helper must still catch the RuntimeError as a
        defense-in-depth measure so a future third caller missing the
        gate doesn't get an uncaught 500.
        """
        from ha_mcp.settings_ui import _supervisor_fetch_current_options

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        # Without the env var, make_supervisor_httpx_client raises before
        # any HTTP call — no patching needed.
        options, err = await _supervisor_fetch_current_options(verify_ssl=True)
        assert options == {}
        assert err is not None
        assert err.kind == "transport"
        assert err.status_code == 502
        assert "Supervisor client unavailable" in err.message

    @pytest.mark.asyncio
    async def test_merge_and_post_options_preserves_existing_keys(self, monkeypatch):
        """The merge must preserve untouched required keys (most importantly
        ``backup_hint``, which has no ``?`` in the addon schema and would
        cause supervisor to reject the entire POST with 400 if omitted).

        Pins the fix for the bug where ``_save_backup_config`` was POSTing
        only the auto-backup fields and dropping ``backup_hint`` in the
        process — exact reproduction of the user-reported failure:
        ``addon_configuration_invalid_error: Missing option 'backup_hint'
        in root``.
        """
        from ha_mcp.settings_ui import _supervisor_merge_and_post_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json = MagicMock(
            return_value={
                "data": {
                    "options": {
                        "backup_hint": "normal",
                        "enable_tool_search": False,
                        "verify_ssl": True,
                        "auto_backup_throttle_minutes": 0,
                        "auto_backup_retain_per_entity": 100,
                        "enable_auto_backup": True,
                    }
                }
            }
        )
        post_response = MagicMock()
        post_response.status_code = 200
        patcher, mock_client = self._patch_supervisor_client(
            get_response=get_response, post_response=post_response
        )
        with patcher:
            ok, err = await _supervisor_merge_and_post_options(
                verify_ssl=True,
                field_changes={
                    "enable_auto_backup": False,
                    "auto_backup_throttle_minutes": 5,
                },
            )

        assert ok is True
        assert err is None
        # The POST body must contain backup_hint (unchanged) + the new values.
        mock_client.post.assert_awaited_once()
        post_call = mock_client.post.call_args
        assert post_call.args[0] == "/addons/self/options"
        body = post_call.kwargs["json"]
        merged = body["options"]
        assert merged["backup_hint"] == "normal"  # preserved
        assert merged["verify_ssl"] is True  # preserved
        assert merged["enable_tool_search"] is False  # preserved
        assert merged["auto_backup_retain_per_entity"] == 100  # preserved
        assert merged["enable_auto_backup"] is False  # changed
        assert merged["auto_backup_throttle_minutes"] == 5  # changed

    @pytest.mark.asyncio
    async def test_merge_and_post_supervisor_400_returns_validation_error(
        self, monkeypatch
    ):
        """Supervisor 4xx on the POST is classified ``kind="validation"``
        and supervisor's real status code is preserved. The route maps
        validation errors to CONFIG_VALIDATION_FAILED and uses
        supervisor's status_code (NOT 502) so the UI shows the actual
        4xx. Collapsing transport + validation into a single 502 sent
        users down the wrong recovery path in the previous version.
        """
        from ha_mcp.settings_ui import _supervisor_merge_and_post_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json = MagicMock(return_value={"data": {"options": {}}})
        post_response = MagicMock()
        post_response.status_code = 400
        post_response.text = (
            "App has invalid options: Missing option 'backup_hint' in root"
        )
        patcher, _ = self._patch_supervisor_client(
            get_response=get_response, post_response=post_response
        )
        with patcher:
            ok, err = await _supervisor_merge_and_post_options(
                verify_ssl=True, field_changes={"enable_auto_backup": True}
            )
        assert ok is False
        assert err is not None
        assert err.kind == "validation"
        assert err.status_code == 400  # supervisor's status, NOT 502
        assert "backup_hint" in err.message

    @pytest.mark.asyncio
    async def test_merge_and_post_transport_error_returns_transport_kind(
        self, monkeypatch
    ):
        """``httpx.HTTPError`` on the POST (network drop / DNS failure /
        supervisor unreachable) must be classified ``kind="transport"``
        with status_code=502 so the route maps it to CONNECTION_FAILED.
        """
        from ha_mcp.settings_ui import _supervisor_merge_and_post_options

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json = MagicMock(return_value={"data": {"options": {}}})
        patcher, _ = self._patch_supervisor_client(
            get_response=get_response,
            post_side_effect=httpx.ConnectError("no route"),
        )
        with patcher:
            ok, err = await _supervisor_merge_and_post_options(
                verify_ssl=True, field_changes={"enable_auto_backup": True}
            )
        assert ok is False
        assert err is not None
        assert err.kind == "transport"
        assert err.status_code == 502

    @pytest.mark.asyncio
    async def test_schedule_self_restart_fires_task_after_delay(self, monkeypatch):
        """``_schedule_supervisor_self_restart`` fires a background task
        that posts to ``/addons/self/restart`` after ``delay``. Override
        the delay to 0 and deterministically wait on the scheduled task
        rather than ``asyncio.sleep``-ing — sleep is flaky on slow CI
        runners.
        """
        from ha_mcp.settings_ui import (
            _BACKGROUND_RESTART_TASKS,
            _schedule_supervisor_self_restart,
        )

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        post_response = MagicMock()
        post_response.status_code = 200
        patcher, mock_client = self._patch_supervisor_client(
            post_response=post_response
        )
        with patcher:
            _schedule_supervisor_self_restart(verify_ssl=True, delay=0)
            # Deterministic wait — gather the strong-ref set the helper
            # maintains rather than relying on a fixed sleep.
            await _drain_background_restart_tasks()

        mock_client.post.assert_awaited_with("/addons/self/restart")
        # Strong-ref set self-clears via add_done_callback once the task
        # finishes — pin that contract too.
        assert set() == _BACKGROUND_RESTART_TASKS

    @pytest.mark.asyncio
    async def test_schedule_self_restart_swallows_connection_drop(self, monkeypatch):
        """Self-restart deliberately kills the addon process — the
        supervisor httpx call is expected to error out with
        ReadError / RemoteProtocolError mid-flight. The helper must
        swallow these without logging at ERROR (we are mid-restart, by
        design).
        """
        from ha_mcp.settings_ui import _schedule_supervisor_self_restart

        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        patcher, _ = self._patch_supervisor_client(
            post_side_effect=httpx.ReadError("killed mid-call")
        )
        with patcher:
            _schedule_supervisor_self_restart(verify_ssl=True, delay=0)
            await _drain_background_restart_tasks()
        # No assertion — passing means the swallowed exception didn't
        # propagate out of the background task.

    @pytest.mark.asyncio
    async def test_schedule_self_restart_catches_runtime_error(
        self, monkeypatch, caplog
    ):
        """``make_supervisor_httpx_client`` raises ``RuntimeError`` when
        ``SUPERVISOR_TOKEN`` is unset. The two supervisor *options*
        helpers already catch this; the schedule helper used to let it
        escape as an uncaught task exception (asyncio surfaces it as
        "Task exception was never retrieved" at GC time, server-visible
        but not the loud ERROR-line of the other failure modes). Pin
        the parity catch so the user gets a clear log when the rare
        race fires.
        """
        import logging

        from ha_mcp.settings_ui import _schedule_supervisor_self_restart

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        with caplog.at_level(logging.ERROR, logger="ha_mcp.settings_ui"):
            _schedule_supervisor_self_restart(verify_ssl=True, delay=0)
            await _drain_background_restart_tasks()

        # Look for the specific log message we expect — broader
        # "any ERROR record" would let unrelated noise pass the test.
        assert any(
            "SUPERVISOR_TOKEN unset" in rec.message
            for rec in caplog.records
            if rec.levelno >= logging.ERROR
        ), "expected a logged ERROR mentioning the missing token"


class TestSaveBackupConfigAddonMode:
    """Addon-mode ``POST /api/settings/backup-config`` flow.

    Pins the fix for the user-reported supervisor 400 — the old code
    POSTed only the three auto-backup fields, dropping ``backup_hint``
    (the addon schema's only required key) and producing
    ``addon_configuration_invalid_error``. The new flow merges through
    ``_supervisor_merge_and_post_options`` and schedules a restart via
    ``_schedule_supervisor_self_restart``.
    """

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_post_handler(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/backup-config") and "POST" in methods:
                    captured["post"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured["post"]

    @pytest.mark.asyncio
    async def test_addon_save_merges_without_restart_returns_restart_required(
        self, monkeypatch
    ):
        """Unified restart flow: every save endpoint (Tools, Server
        Settings, Backups) commits the change but **does not** fire the
        addon restart. The user picks when to restart via the global
        Restart Add-on button. The save response carries
        ``restart_required=True`` so the cross-tab banner appears.

        Pins the contract — a regression that re-introduces an
        auto-restart from inside the save handler races the supervisor
        kill against the JSON response flush and surfaces a spurious
        "Restart failed" alert / "addon is restarting" message.
        """
        post_handler = self._capture_post_handler(monkeypatch)

        merge_mock = AsyncMock(return_value=(True, None))
        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._supervisor_merge_and_post_options", merge_mock
        )
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart", schedule_mock
        )

        resp = await post_handler(
            self._make_request(
                {"enable_auto_backup": True, "auto_backup_throttle_minutes": 5}
            )
        )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["mode"] == "addon"
        assert body["restart_required"] is True
        assert "restarting" not in body  # legacy field name must be gone
        merge_mock.assert_awaited_once_with(
            True,
            {"enable_auto_backup": True, "auto_backup_throttle_minutes": 5},
        )
        schedule_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_addon_save_surfaces_validation_error_with_supervisor_status(
        self, monkeypatch
    ):
        """Supervisor schema rejection (``kind="validation"``) must surface
        as ``CONFIG_VALIDATION_FAILED`` with supervisor's real status
        code preserved (not a generic 502). The restart must NOT fire
        if the options write itself failed.
        """
        from ha_mcp.settings_ui import _SupervisorOptionsError

        post_handler = self._capture_post_handler(monkeypatch)

        merge_mock = AsyncMock(
            return_value=(
                False,
                _SupervisorOptionsError(
                    kind="validation",
                    message="Supervisor rejected (400): bad schema",
                    status_code=400,
                ),
            )
        )
        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._supervisor_merge_and_post_options", merge_mock
        )
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart", schedule_mock
        )

        resp = await post_handler(self._make_request({"enable_auto_backup": True}))

        assert resp.status_code == 400  # supervisor's status, not 502
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_FAILED"
        assert "bad schema" in body["error"]["message"]
        schedule_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_addon_save_surfaces_transport_error_as_connection_failed(
        self, monkeypatch
    ):
        """Transport-class failures (``kind="transport"``) get mapped to
        ``CONNECTION_FAILED`` with 502 — distinct recovery path from the
        validation case (the UI shows "is HA reachable" suggestions
        instead of schema-recovery suggestions).
        """
        from ha_mcp.settings_ui import _SupervisorOptionsError

        post_handler = self._capture_post_handler(monkeypatch)

        merge_mock = AsyncMock(
            return_value=(
                False,
                _SupervisorOptionsError(
                    kind="transport",
                    message="Could not reach Supervisor: connect refused",
                    status_code=502,
                ),
            )
        )
        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._supervisor_merge_and_post_options", merge_mock
        )
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart", schedule_mock
        )

        resp = await post_handler(self._make_request({"enable_auto_backup": True}))

        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "CONNECTION_FAILED"
        schedule_mock.assert_not_called()


class TestSaveFeatureFlagsAddonMode:
    """Addon-mode ``POST /api/settings/features`` flow.

    Mirror of TestSaveBackupConfigAddonMode for feature flags. In addon
    mode, ``get_feature_flag_origin`` returns ``"addon"`` for non-beta
    flags (because SUPERVISOR_TOKEN is set), so the handler routes
    through Supervisor instead of refusing the write or persisting to
    the override file (the file is ignored in addon mode for those —
    ``start.py`` rewrites env vars from ``config.yaml`` on every boot).

    Beta sub-flags in addon mode have channel-dependent behavior (see
    ``get_feature_flag_origin`` docstring): dev addon → Supervisor;
    stable addon → file. These tests use ``enable_tool_search`` which
    is non-beta and routes identically in both channels.
    """

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_post_handler(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/features") and "POST" in methods:
                    captured["post"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured["post"]

    @pytest.mark.asyncio
    async def test_addon_save_merges_without_restart_returns_restart_required(
        self, monkeypatch
    ):
        """Mirror of TestSaveBackupConfigAddonMode's counterpart — see
        that test for the unified-restart-flow rationale.
        """
        post_handler = self._capture_post_handler(monkeypatch)

        merge_mock = AsyncMock(return_value=(True, None))
        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._supervisor_merge_and_post_options", merge_mock
        )
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart", schedule_mock
        )

        resp = await post_handler(
            self._make_request({"flags": {"enable_tool_search": True}})
        )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["mode"] == "addon"
        assert body["restart_required"] is True
        assert "restarting" not in body
        merge_mock.assert_awaited_once_with(True, {"enable_tool_search": True})
        schedule_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_addon_save_surfaces_validation_error_with_supervisor_status(
        self, monkeypatch
    ):
        """Mirror of the backup-config test: supervisor schema rejection
        must surface as CONFIG_VALIDATION_FAILED with supervisor's real
        status code, not a generic 502. (e.g. attempting to set a
        beta-only flag on the production addon channel where the schema
        doesn't include it.)
        """
        from ha_mcp.settings_ui import _SupervisorOptionsError

        post_handler = self._capture_post_handler(monkeypatch)

        merge_mock = AsyncMock(
            return_value=(
                False,
                _SupervisorOptionsError(
                    kind="validation",
                    message="Supervisor rejected (400): unknown option",
                    status_code=400,
                ),
            )
        )
        schedule_mock = MagicMock()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._supervisor_merge_and_post_options", merge_mock
        )
        monkeypatch.setattr(
            "ha_mcp.settings_ui._schedule_supervisor_self_restart", schedule_mock
        )

        resp = await post_handler(
            self._make_request({"flags": {"enable_tool_search": True}})
        )

        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_FAILED"
        schedule_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_addon_save_returns_500_when_server_is_none(self, monkeypatch):
        """Defensive guard for the stdio-sidecar shape. ``server is None``
        means the handler was constructed without a live MCP server
        (the sidecar process); addon detection should already be False
        there, so this branch is type-checker + future-refactor safety
        net rather than a user-visible code path. Pin it so removal
        becomes a deliberate decision.
        """
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/features") and "POST" in methods:
                    captured["post"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        # server=None — the sidecar shape.
        register_settings_routes(mcp, None, secret_path="/x")

        resp = await captured["post"](
            self._make_request({"flags": {"enable_tool_search": True}})
        )

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["success"] is False
        assert body["error"]["code"] == "INTERNAL_ERROR"


class TestSaveFeatureFlagsStandaloneMode:
    """Non-addon ``POST /api/settings/features`` flow.

    Pins the file/default-mode response shape introduced when the save
    handlers unified on ``restart_required``. The JS in
    ``saveFeatureFlag`` branches on ``data.restart_required`` to show
    the cross-tab restart banner; a regression to the old bare
    ``{"success": True}`` shape would silently hide the banner for
    every standalone / Docker / Claude Desktop user. Lock the shape.
    """

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_post_handler(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        # Point feature-flag persistence at the test's temp dir so we
        # don't write to the real data dir.
        monkeypatch.setattr("ha_mcp.utils.data_paths.get_data_dir", lambda: tmp_path)
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/features") and "POST" in methods:
                    captured["post"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured["post"]

    @pytest.mark.asyncio
    async def test_standalone_save_returns_unified_contract_shape(
        self, monkeypatch, tmp_path
    ):
        """File-mode response must match the unified
        ``{success, applied, mode, restart_required}`` shape — same
        keys as Tools and Server Settings save endpoints. The
        ``restart_required: True`` carries the banner cue; ``mode:
        "file"`` distinguishes the persistence path; ``applied``
        echoes the new value(s) so the client can confirm what stuck.
        """
        post_handler = self._capture_post_handler(monkeypatch, tmp_path)

        resp = await post_handler(
            self._make_request({"flags": {"enable_tool_search": True}})
        )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert body["mode"] == "file"
        assert body["restart_required"] is True
        assert body["applied"] == {"enable_yaml_config_editing": True}
        # Legacy field names from earlier iterations must not creep
        # back in alongside the new shape.
        assert "restarting" not in body


class TestSaveToolsResponseShape:
    """Pins the unified ``{success, applied, mode, restart_required}``
    response shape on ``POST /api/settings/tools``. Previously returned
    ``disabled`` and ``pinned`` count fields that no JS or test code
    actually consumed; replaced with ``applied`` + ``mode`` so the
    three save endpoints (Tools, Server Settings, Backups) share the
    same contract and a cross-tab BroadcastChannel listener can react
    uniformly. A regression to either the old counts shape or a
    bare ``{"success": True}`` would break the JS banner.
    """

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_post_handler(self, monkeypatch, tmp_path):
        # Point the tool-config write at the test temp dir.
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/tools") and "POST" in methods:
                    captured["post"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured["post"]

    @pytest.mark.asyncio
    async def test_save_returns_unified_contract_shape(self, monkeypatch, tmp_path):
        post_handler = self._capture_post_handler(monkeypatch, tmp_path)

        resp = await post_handler(
            self._make_request(
                {"states": {"ha_get_state": "pinned", "ha_search_entities": "disabled"}}
            )
        )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert body["mode"] == "file"
        assert body["restart_required"] is True
        assert body["applied"] == {
            "ha_get_state": "pinned",
            "ha_search_entities": "disabled",
        }
        # The retired count fields must not leak through.
        assert "disabled" not in body
        assert "pinned" not in body


class TestSettingsInfoEndpoint:
    """``GET /api/settings/info`` exposes per-process identity so the
    restart-then-reload JS cycle can prove a restart actually happened.

    Without ``instance_id`` the JS poll cycle can't tell the difference
    between "addon successfully restarted and the new instance is up"
    and "addon never restarted (silent supervisor failure) and the OLD
    instance is still serving 200" — both look identical to a status-
    only probe. Pins the contract: ``instance_id`` must be present,
    stable within a process, and ``started_at`` must be a positive
    epoch-seconds float.
    """

    def _capture_handler(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, SaveHandler] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path.endswith("/api/settings/info") and "GET" in methods:
                    captured["get"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        server = MagicMock()
        server.settings.verify_ssl = True
        register_settings_routes(mcp, server, secret_path="/x")
        return captured["get"]

    @pytest.mark.asyncio
    async def test_returns_instance_id_and_started_at(self, monkeypatch):
        from ha_mcp.settings_ui import (
            _PROCESS_INSTANCE_ID,
            _PROCESS_STARTED_AT,
        )

        handler = self._capture_handler(monkeypatch)
        resp = await handler(MagicMock())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        # Pre-existing fields still there.
        assert "is_addon" in body
        assert "is_sidecar" in body
        # New restart-detection fields.
        assert body["instance_id"] == _PROCESS_INSTANCE_ID
        assert body["started_at"] == _PROCESS_STARTED_AT
        # Sanity: instance_id is a non-empty string, started_at is a
        # positive epoch-seconds float (i.e. truly an instant in time,
        # not a serialization artifact).
        assert isinstance(body["instance_id"], str)
        assert len(body["instance_id"]) > 0
        assert isinstance(body["started_at"], int | float)
        assert body["started_at"] > 0

    @pytest.mark.asyncio
    async def test_instance_id_stable_within_process(self, monkeypatch):
        """Two calls within the same process must return the same
        ``instance_id``. Without this invariant the JS poll cycle
        would see the value flip on every call and reload immediately,
        completely defeating the restart-detection contract.
        """
        handler = self._capture_handler(monkeypatch)
        first = json.loads((await handler(MagicMock())).body)
        second = json.loads((await handler(MagicMock())).body)
        assert first["instance_id"] == second["instance_id"]
        assert first["started_at"] == second["started_at"]


class TestFeatureGatedToolsCustomCode:
    """``ha_manage_custom_tool`` (gated by ``enable_code_mode``) must
    appear in the settings-UI tool list when the toggle is off — same
    pattern as ``ha_config_set_yaml`` — so users discover the beta
    feature and how to enable it. Pins the fix for the asymmetry the
    user reported.
    """

    def test_custom_code_tool_is_listed(self):
        from ha_mcp.settings_ui import FEATURE_GATED_TOOLS

        assert "ha_manage_custom_tool" in FEATURE_GATED_TOOLS
        entry = FEATURE_GATED_TOOLS["ha_manage_custom_tool"]
        # The "Beta — set X" hint copy is keyed off ``disabled_by`` —
        # without this the JS template renders no hint at all.
        assert entry["disabled_by"] == "enable_code_mode"  # type: ignore[typeddict-item]
        # Lives in the System group, matching ha_config_set_yaml so the
        # related beta tools render together.
        assert entry["primary_tag"] == "System"


class TestEnvPinnedTools:
    """Tests for per-tool env-pin enforcement (#1164 addendum)."""

    def test_env_pinned_tools_helper_returns_correct_mapping(self, monkeypatch):
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo, ha_bar")
        monkeypatch.setenv("PINNED_TOOLS", "ha_baz, ha_qux")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import env_pinned_tools

        _reset_global_settings()
        pinned = env_pinned_tools()
        assert pinned == {
            "ha_foo": "disabled",
            "ha_bar": "disabled",
            "ha_baz": "pinned",
            "ha_qux": "pinned",
        }
        _reset_global_settings()

    def test_env_pinned_tools_pinned_wins_on_collision(self, monkeypatch):
        """If a name appears in both DISABLED_TOOLS and PINNED_TOOLS, pinned
        wins (matches seed semantics in load_tool_config)."""
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo")
        monkeypatch.setenv("PINNED_TOOLS", "ha_foo")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import env_pinned_tools

        _reset_global_settings()
        assert env_pinned_tools() == {"ha_foo": "pinned"}
        _reset_global_settings()

    def test_env_pinned_disabled_tool_stays_disabled_even_after_file_write(
        self, monkeypatch, tmp_path
    ):
        """DISABLED_TOOLS=ha_foo + tool_config.json says ha_foo='enabled' →
        runtime sees ha_foo as disabled (env wins per-tool)."""
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo")
        monkeypatch.delenv("PINNED_TOOLS", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_foo": "enabled"}})
        )
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import effective_tool_config

        _reset_global_settings()
        cfg = effective_tool_config()
        assert cfg["tools"]["ha_foo"] == "disabled"
        get_data_dir.cache_clear()

    def test_env_pinned_pinned_tool_stays_pinned_even_after_file_write(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("PINNED_TOOLS", "ha_bar")
        monkeypatch.delenv("DISABLED_TOOLS", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_bar": ""}})
        )
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import effective_tool_config

        _reset_global_settings()
        cfg = effective_tool_config()
        assert cfg["tools"]["ha_bar"] == "pinned"
        get_data_dir.cache_clear()

    def test_non_env_pinned_tool_remains_freely_editable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo")
        monkeypatch.delenv("PINNED_TOOLS", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_other": "disabled"}})
        )
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import effective_tool_config

        _reset_global_settings()
        cfg = effective_tool_config()
        assert cfg["tools"]["ha_foo"] == "disabled"  # env-pinned
        assert cfg["tools"]["ha_other"] == "disabled"  # file-set, still editable in UI
        get_data_dir.cache_clear()

    @pytest.mark.asyncio
    async def test_save_tools_rejects_env_pinned_tool_flip(self, monkeypatch, tmp_path):
        """POST attempting to flip an env-pinned tool returns 409."""
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        request = MagicMock()
        request.json = AsyncMock(return_value={"states": {"ha_foo": "enabled"}})
        resp = await handlers["save_tools"](request)
        assert resp.status_code == 409
        body = json.loads(resp.body)
        assert body["success"] is False
        assert "ha_foo" in str(body)
        get_data_dir.cache_clear()
        _reset_global_settings()

    @pytest.mark.asyncio
    async def test_get_tools_includes_env_pinned_map(self, monkeypatch):
        """GET /api/settings/tools advertises env_pinned status so UI can
        render locked rows in Chunk 5b."""
        monkeypatch.setenv("DISABLED_TOOLS", "ha_foo")
        monkeypatch.delenv("PINNED_TOOLS", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        resp = await handlers["get_tools"](MagicMock())
        body = json.loads(resp.body)
        assert body["env_pinned"] == {"ha_foo": "disabled"}
        # Also confirm the overlay is reflected in states / tools.
        assert body["states"].get("ha_foo") == "disabled"
        _reset_global_settings()


class TestAdvancedSettingsEndpoints:
    """/api/settings/advanced GET+POST handlers (#1164 Chunk 2a)."""

    @pytest.mark.asyncio
    async def test_get_advanced_returns_all_registered_fields(self, monkeypatch):
        from ha_mcp.config import (
            ADVANCED_SETTINGS_FIELDS,
            _reset_global_settings,
        )
        from ha_mcp.settings_ui import build_settings_handlers

        # Clear every advanced env var so origins resolve cleanly.
        for _fname, ename, *_ in ADVANCED_SETTINGS_FIELDS:
            monkeypatch.delenv(ename, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        resp = await handlers["get_advanced_settings"](MagicMock())
        assert resp.status_code == 200
        body = json.loads(resp.body)
        field_names = {f["field"] for f in body["fields"]}
        assert "homeassistant_url" in field_names
        assert "log_level" in field_names
        assert "verify_ssl" in field_names

    @pytest.mark.asyncio
    async def test_get_advanced_marks_connection_fields_display_only(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        resp = await handlers["get_advanced_settings"](MagicMock())
        body = json.loads(resp.body)
        url_row = next(f for f in body["fields"] if f["field"] == "homeassistant_url")
        assert url_row["editable"] is False
        assert url_row["section"] == "connection"

    @pytest.mark.asyncio
    async def test_get_advanced_log_level_has_choices(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        resp = await handlers["get_advanced_settings"](MagicMock())
        body = json.loads(resp.body)
        log_row = next(f for f in body["fields"] if f["field"] == "log_level")
        assert log_row["choices"] == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @pytest.mark.asyncio
    async def test_get_advanced_masks_token_when_set(self, monkeypatch, tmp_path):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "real-secret-jwt-value")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        resp = await handlers["get_advanced_settings"](MagicMock())
        body = json.loads(resp.body)
        token_row = next(
            f for f in body["fields"] if f["field"] == "homeassistant_token"
        )
        assert token_row["value"] == "*****"
        assert "real-secret-jwt-value" not in str(body)

    @pytest.mark.asyncio
    async def test_save_advanced_rejects_unknown_field(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"made_up_field": 42})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "made_up_field" in str(body)

    @pytest.mark.asyncio
    async def test_save_advanced_rejects_display_only_field(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"homeassistant_url": "http://attacker"})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_save_advanced_rejects_env_pinned_field(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.setenv("HA_TIMEOUT", "60")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"timeout": 90})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 409
        body = json.loads(resp.body)
        assert "HA_TIMEOUT" in str(body)

    @pytest.mark.asyncio
    async def test_save_advanced_rejects_out_of_bounds(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("HA_TIMEOUT", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"timeout": -5})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_save_advanced_rejects_invalid_choice(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"log_level": "BANANAS"})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_save_advanced_happy_path(self, monkeypatch, tmp_path):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("HA_TIMEOUT", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        get_data_dir.cache_clear()
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"timeout": 90})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert body["applied"] == {"timeout": 90}
        assert body["restart_required"] is True
        # File written and re-readable by Settings on next construct.
        from ha_mcp.config import get_global_settings

        assert get_global_settings().timeout == 90
        get_data_dir.cache_clear()
        _reset_global_settings()

    @pytest.mark.asyncio
    async def test_save_advanced_rejects_null_byte_in_str(self, monkeypatch):
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("MCP_SERVER_NAME", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"mcp_server_name": "evil\x00name"})
        resp = await handlers["save_advanced_settings"](req)
        assert resp.status_code == 400


class TestBetaMasterGateInSave:
    """Server-side rejection of beta sub-flag writes when master is off (#1164 Chunk 3a)."""

    @pytest.mark.asyncio
    async def test_save_features_rejects_beta_subflag_when_master_off(
        self, monkeypatch
    ):
        """POST {enable_yaml_config_editing: true} while master is off → 409."""
        from ha_mcp.config import FEATURE_FLAG_FIELDS, _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        for _fname, ename, _ftype in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(ename, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(
            return_value={"flags": {"enable_yaml_config_editing": True}}
        )
        resp = await handlers["save_feature_flags"](req)
        assert resp.status_code == 409
        body = json.loads(resp.body)
        assert "enable_yaml_config_editing" in str(body)

    @pytest.mark.asyncio
    async def test_save_features_accepts_master_and_subflag_in_same_batch(
        self, monkeypatch, tmp_path
    ):
        """POST {enable_beta_features: true, enable_yaml_config_editing: true}
        in one batch succeeds — effective master state is derived AFTER merge."""
        from ha_mcp.config import FEATURE_FLAG_FIELDS, _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        get_data_dir.cache_clear()
        for _fname, ename, _ftype in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(ename, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(
            return_value={
                "flags": {
                    "enable_beta_features": True,
                    "enable_yaml_config_editing": True,
                }
            }
        )
        resp = await handlers["save_feature_flags"](req)
        # Must NOT be 409 — both arrive in one batch so effective master = True.
        assert resp.status_code == 200
        get_data_dir.cache_clear()
        _reset_global_settings()

    @pytest.mark.asyncio
    async def test_save_features_allows_subflag_when_master_already_on(
        self, monkeypatch, tmp_path
    ):
        """If feature_flags.json already has master=True, a sub-flag save
        with no master in the payload succeeds (no 409)."""
        from ha_mcp.config import FEATURE_FLAG_FIELDS, _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        get_data_dir.cache_clear()
        for _fname, ename, _ftype in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(ename, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        (tmp_path / "feature_flags.json").write_text(
            json.dumps({"enable_beta_features": True})
        )
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        req = MagicMock()
        req.json = AsyncMock(return_value={"flags": {"enable_filesystem_tools": True}})
        resp = await handlers["save_feature_flags"](req)
        assert resp.status_code == 200
        get_data_dir.cache_clear()
        _reset_global_settings()
