"""Unit tests for ha_config_set_yaml MCP tool wrapper."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def enable_flag(monkeypatch):
    """Enable the yaml-config tool flag and bust the cached global settings.

    `get_global_settings()` memoizes a `Settings` object the first time it's called.
    If anything in the test process imported the module before this fixture ran,
    the cached settings have ENABLE_YAML_CONFIG_EDITING=False and our env var is
    ignored. Reset the cache before AND after to keep tests hermetic.

    The master beta gate also force-sets every beta sub-flag False at
    runtime when ``ENABLE_BETA_FEATURES`` is unset, so set both env
    vars together — otherwise the cached settings would land
    with ``enable_yaml_config_editing=False`` regardless of the
    sub-flag env var.
    """
    from ha_mcp import config as ha_mcp_config

    monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "true")
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setattr(ha_mcp_config, "_settings", None)
    yield
    # Reset the cache so other tests don't see our enabled flag.
    ha_mcp_config._settings = None


@pytest.fixture(autouse=True)
def _reset_caller_token_cache():
    """The wrapper now caches the bootstrap token per-client. Each test gets
    a fresh client, so the cache must be reset to avoid stale entries from
    a previously-recycled id()."""
    from ha_mcp.tools.tools_filesystem import _reset_caller_token_cache

    _reset_caller_token_cache()
    yield
    _reset_caller_token_cache()


def _build_call_service_mock():
    """Make a call_service mock that satisfies the bootstrap fetch + dispatch.

    The wrapper does two service calls per tool invocation now:
      1. ha_mcp_tools.get_caller_token → returns the token
      2. ha_mcp_tools.<actual_service> → returns the tool's response
    """

    async def fake_call_service(domain, service, payload, **kwargs):
        if service == "get_caller_token":
            # ``version`` is required by the ha-mcp MIN_COMPONENT_VERSION
            # gate (added with packages-only-keys PR). Use the current
            # minimum so the test setup matches what a freshly-installed
            # component would return.
            from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

            return {
                "service_response": {
                    "success": True,
                    "token": "test-token",
                    "version": MIN_COMPONENT_VERSION,
                }
            }
        return {"success": True, "file": "configuration.yaml"}

    mock = AsyncMock(side_effect=fake_call_service)
    return mock


async def _make_tool():
    """Build a minimal mcp + client harness around register_yaml_config_tools."""
    from ha_mcp.tools.tools_yaml_config import register_yaml_config_tools

    captured: dict = {}

    class FakeMCP:
        def add_tool(self, method):
            captured.setdefault("fns", []).append(method)

    client = MagicMock()
    # _fetch_caller_token pre-flights /api/services and requires
    # get_caller_token to be registered — list it alongside the other
    # services so the bootstrap doesn't trip COMPONENT_NOT_INSTALLED.
    client.get_services = AsyncMock(
        return_value=[
            {
                "domain": "ha_mcp_tools",
                "services": {
                    "get_caller_token": {},
                    "edit_yaml_config": {},
                },
            }
        ]
    )
    client.send_websocket_message = AsyncMock()
    client.call_service = _build_call_service_mock()

    mcp = FakeMCP()
    register_yaml_config_tools(mcp, client)
    # Find the ha_config_set_yaml fn — only one tool registered in this module
    return captured["fns"][0], client


def _dispatch_call_count(client) -> int:
    """Count call_service invocations that aren't the bootstrap fetch.

    With caller-token auth, every tool invocation makes 2 calls (bootstrap +
    actual service) on first use, 1 (just the actual) afterward. Tests want
    to count just the dispatched-to-ha_mcp_tools.<dangerous-service> calls.
    """
    return sum(
        1
        for c in client.call_service.await_args_list
        if c.args[1] != "get_caller_token"
    )


async def test_storage_collision_blocks_dispatch(monkeypatch):
    """If WS list shows a storage-mode dashboard with same url_path, reject."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [{"url_path": "energy-dash", "mode": "storage", "id": "abc"}]
        }
    )

    # ToolError is raised — capture it
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await fn(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
        )
    # call_service must NOT have been called
    client.call_service.assert_not_called()


async def test_no_collision_dispatches(monkeypatch):
    """No matching storage-mode entry — dispatch proceeds."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [{"url_path": "other-dash", "mode": "storage", "id": "abc"}]
        }
    )
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    assert _dispatch_call_count(client) == 1


async def test_non_dashboard_path_skips_ws_check(monkeypatch):
    """Single-key yaml_paths must not trigger the WS lookup."""
    fn, client = await _make_tool()
    await fn(
        yaml_path="template",
        action="add",
        content="- sensor: []\n",
    )
    client.send_websocket_message.assert_not_called()
    assert _dispatch_call_count(client) == 1


async def test_ws_failure_skips_check_and_dispatches(monkeypatch):
    """WS query failure must warn-and-skip, not block dispatch."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(side_effect=ConnectionError("boom"))
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    assert _dispatch_call_count(client) == 1


async def test_ws_returns_bare_list_blocks_collision(monkeypatch):
    """WS may return a bare list (no 'result' wrapper); collision still detected."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value=[{"url_path": "energy-dash", "mode": "storage", "id": "abc"}]
    )

    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await fn(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
        )
    client.call_service.assert_not_called()


async def test_yaml_mode_existing_does_not_block(monkeypatch):
    """Existing yaml-mode entry with same url_path is NOT a collision; dispatch proceeds.
    (HA itself surfaces dup errors at config_check time.)"""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [{"url_path": "energy-dash", "mode": "yaml", "id": "abc"}]
        }
    )
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    assert _dispatch_call_count(client) == 1


async def test_ws_returns_unexpected_shape_warns_and_dispatches(monkeypatch):
    """Unexpected WS response shape (non-dict, non-list) skips collision check."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(return_value="weird")
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    assert _dispatch_call_count(client) == 1


async def test_remove_action_skips_collision_check(monkeypatch):
    """`remove` must NOT pay the WS round-trip — users need to be able to
    clean up YAML entries even when a storage-mode dashboard owns the same
    url_path (migration scenario)."""
    fn, client = await _make_tool()
    # Set up the collision return so we'd notice if the check ran.
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [{"url_path": "energy-dash", "mode": "storage", "id": "abc"}]
        }
    )
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="remove",
    )
    client.send_websocket_message.assert_not_called()
    assert _dispatch_call_count(client) == 1


# ---------------------------------------------------------------------------
# Per-key gates for PACKAGES_ONLY_YAML_KEYS (automation/script/scene)
# ---------------------------------------------------------------------------


def _dispatch_payloads(client) -> list[dict]:
    """Return the service_data dicts the wrapper actually posted to
    ha_mcp_tools.<dangerous-service>, skipping the bootstrap fetch."""
    return [
        c.args[2]
        for c in client.call_service.await_args_list
        if c.args[1] != "get_caller_token"
    ]


@pytest.mark.parametrize(
    "key,flag",
    [
        ("automation", "ENABLE_YAML_PACKAGES_AUTOMATION"),
        ("script", "ENABLE_YAML_PACKAGES_SCRIPT"),
        ("scene", "ENABLE_YAML_PACKAGES_SCENE"),
    ],
)
async def test_disabled_key_rejects_client_side(monkeypatch, key, flag):
    """With the per-key flag OFF, the wrapper must reject before the
    call ever reaches the custom component. The other PACKAGES_ONLY
    keys with their flag ON keep working in the same test process."""
    from fastmcp.exceptions import ToolError

    from ha_mcp import config as ha_mcp_config

    # Leave the other two flags ON so we can confirm the reject is
    # per-key, not a blanket "all packages disabled" mode.
    for other_flag in (
        "ENABLE_YAML_PACKAGES_AUTOMATION",
        "ENABLE_YAML_PACKAGES_SCRIPT",
        "ENABLE_YAML_PACKAGES_SCENE",
    ):
        monkeypatch.setenv(other_flag, "true")
    monkeypatch.delenv(flag, raising=False)
    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    fn, client = await _make_tool()
    with pytest.raises(ToolError) as excinfo:
        await fn(
            file=f"packages/{key}.yaml",
            yaml_path=key,
            action="add",
            content=f"- alias: example_{key}\n  trigger: []\n  action: []\n",
        )
    # Error message must name the disabled key so a reader can act.
    assert key in str(excinfo.value)
    # call_service must NOT have been called for the dispatch
    # (bootstrap fetch is OK; that fires before any reject).
    assert _dispatch_call_count(client) == 0


async def test_enabled_key_passes_disabled_set_in_payload(monkeypatch):
    """When all 3 flags are ON, the wrapper still passes a (empty)
    ``disabled_packages_keys`` list so the component receives the
    field consistently. When some are OFF, the disabled ones appear
    in the list — that's the defense-in-depth payload."""
    from ha_mcp import config as ha_mcp_config

    # automation ON, script ON, scene OFF.
    monkeypatch.setenv("ENABLE_YAML_PACKAGES_AUTOMATION", "true")
    monkeypatch.setenv("ENABLE_YAML_PACKAGES_SCRIPT", "true")
    monkeypatch.delenv("ENABLE_YAML_PACKAGES_SCENE", raising=False)
    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    fn, client = await _make_tool()
    await fn(
        file="packages/auto.yaml",
        yaml_path="automation",
        action="add",
        content="- alias: example\n  trigger: []\n  action: []\n",
    )
    payloads = _dispatch_payloads(client)
    assert len(payloads) == 1
    assert payloads[0].get("disabled_packages_keys") == ["scene"]


async def test_non_packages_key_unaffected_by_flag(monkeypatch):
    """Keys that aren't PACKAGES_ONLY (e.g. ``template``) must not be
    gated by these flags. They route through the same wrapper but
    aren't in _YAML_PACKAGES_FLAG_BY_KEY."""
    from ha_mcp import config as ha_mcp_config

    # Turn ALL 3 packages flags OFF.
    for f in (
        "ENABLE_YAML_PACKAGES_AUTOMATION",
        "ENABLE_YAML_PACKAGES_SCRIPT",
        "ENABLE_YAML_PACKAGES_SCENE",
    ):
        monkeypatch.delenv(f, raising=False)
    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    fn, client = await _make_tool()
    await fn(
        yaml_path="template",
        action="add",
        content="- sensor: []\n",
    )
    assert _dispatch_call_count(client) == 1
    # The disabled set is still computed and forwarded (defense in depth),
    # in deterministic sorted order, even though ``template`` isn't gated.
    assert _dispatch_payloads(client)[0].get("disabled_packages_keys") == [
        "automation",
        "scene",
        "script",
    ]


async def test_disabled_key_in_configuration_yaml_falls_through(monkeypatch):
    """A disabled PACKAGES_ONLY key targeting configuration.yaml must NOT be
    rejected client-side — the wrapper gate is scoped to packages/*.yaml. The
    call is forwarded (the component rejects it for being a packages-only key),
    and the disabled set is still attached for the component's own gate."""
    from ha_mcp import config as ha_mcp_config

    monkeypatch.delenv("ENABLE_YAML_PACKAGES_AUTOMATION", raising=False)
    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    fn, client = await _make_tool()
    await fn(
        file="configuration.yaml",
        yaml_path="automation",
        action="add",
        content="- alias: x\n  trigger: []\n  action: []\n",
    )
    # Gate did NOT fire client-side (it would have raised + skipped dispatch).
    assert _dispatch_call_count(client) == 1
    assert "automation" in _dispatch_payloads(client)[0].get(
        "disabled_packages_keys", []
    )


async def test_relative_packages_path_is_normalized_and_gated(monkeypatch):
    """``./packages/x.yaml`` normalises to ``packages/x.yaml`` (matching the
    component's os.path.normpath + fnmatch classification), so the disabled-key
    gate fires client-side for it too — not only the bare ``packages/`` spelling."""
    from fastmcp.exceptions import ToolError

    from ha_mcp import config as ha_mcp_config

    monkeypatch.delenv("ENABLE_YAML_PACKAGES_AUTOMATION", raising=False)
    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    fn, client = await _make_tool()
    with pytest.raises(ToolError) as excinfo:
        await fn(
            file="./packages/auto.yaml",
            yaml_path="automation",
            action="add",
            content="- alias: x\n  trigger: []\n  action: []\n",
        )
    assert "automation" in str(excinfo.value)
    assert _dispatch_call_count(client) == 0


async def test_disabled_key_remove_action_rejected_before_dispatch(monkeypatch):
    """The gate fires for ``remove`` too (which carries no content), before any
    dispatch — so it can't be bypassed by choosing an action that skips the
    content-required check."""
    from fastmcp.exceptions import ToolError

    from ha_mcp import config as ha_mcp_config

    monkeypatch.delenv("ENABLE_YAML_PACKAGES_SCENE", raising=False)
    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    fn, client = await _make_tool()
    with pytest.raises(ToolError) as excinfo:
        await fn(
            file="packages/scenes.yaml",
            yaml_path="scene",
            action="remove",
        )
    assert "scene" in str(excinfo.value)
    assert _dispatch_call_count(client) == 0
