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
        pinned = apply_tool_visibility(mcp, config, settings)
        assert "ha_restart" in pinned
        assert "ha_hacs_info" not in pinned

    def test_empty_config_no_disable(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_not_called()


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
        block): the Supervisor kills our process mid-request during a
        restart, so the connection-drop is the documented success signal —
        not a failure to surface. ConnectError is excluded because it fires
        BEFORE a connection is established (DNS / TCP refused / socket
        misconfigured) and means Supervisor was unreachable, not that a
        restart was initiated.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request()

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
        which returns 502 with `CONNECTION_FAILED`.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request()

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
        last unconvered transport-error path in `_restart_addon`.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request()

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
        """When Supervisor returns a non-2xx status (e.g. 401 Unauthorized),
        the handler must surface a 502 to the caller — the restart was not
        initiated. Pins the `status_code >= 400` branch in `_restart_addon`.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request()

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
    async def test_posts_relative_url_for_self_restart(self, monkeypatch):
        """No-body request POSTs ``/addons/self/restart`` — the UI button's path."""
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request()

        response = MagicMock()
        response.status_code = 200
        patcher, mock_client = self._patch_supervisor_client(post_return=response)
        with patcher:
            await restart(request)

        mock_client.post.assert_awaited_once_with("/addons/self/restart")

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

        Preserves the historical self-restart behavior when callers post a
        body that doesn't carry a usable slug. The settings-UI restart
        button posts no body at all; the explicit slug paths exist purely
        for the E2E test surface and should never accidentally redirect
        a self-restart to ``/addons//restart`` or similar.
        """
        restart = self._capture_handler(monkeypatch, with_token=True)
        request = self._make_request(body=body)

        response = MagicMock()
        response.status_code = 200
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ha_mcp.settings_ui.httpx.AsyncClient", return_value=cm):
            await restart(request)

        mock_client.post.assert_awaited_once_with("/addons/self/restart")


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
        assert body["restarting"] is False
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
