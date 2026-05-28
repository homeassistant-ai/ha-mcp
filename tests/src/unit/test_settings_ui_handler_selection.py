"""Test ``build_settings_handlers`` selects live-vs-stub policy handlers.

The live policy handlers require a server with an ``approval_queue``
attribute. Sidecar mode, ``server=None``, and a server whose
``approval_queue`` is ``None`` (e.g. ``ENABLE_TOOL_SECURITY_POLICIES``
off so ``_apply_tool_security_policies`` early-returned) all fall back
to stub handlers that 503 on the live ``pending``/``approve``/``deny``
routes.

This is a contract test for the branch at
``settings_ui.build_settings_handlers`` ~ ln 2944
(``if not is_sidecar and approval_queue is not None``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.settings_ui import build_settings_handlers


def _fake_get_request():
    """Minimal stand-in for a Request.

    Both the live ``policy_get_pending`` handler and the stub
    ``unavailable`` handler accept the request as ``_`` and never
    inspect any attribute on it. A MagicMock is sufficient and avoids
    coupling this test to Starlette's ASGI scope schema.
    """
    return MagicMock(name="request")


def _fake_post_request_with_token():
    """Stand-in for a POST request whose JSON body is ``{"token": "nope"}``.

    The live approve/deny handlers call ``await request.json()`` then
    look up ``body["token"]`` — return a coroutine that yields that
    dict so the handler runs through to the queue lookup and returns
    404 (unknown token). The stub handler ignores the body entirely.
    """

    async def _json():
        return {"token": "nope"}

    req = MagicMock(name="request")
    req.json = _json
    return req


@pytest.fixture(autouse=True)
def _reset_data_dir_cache():
    """Match test_settings_ui.py: clear the memoized data-dir between cases."""
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "setup_name,expected_stub_status",
    [
        ("sidecar", 503),
        ("no_server", 503),
        ("no_queue", 503),
        ("live", None),  # None = not stub; live status varies per route
    ],
)
@pytest.mark.parametrize(
    "route_name,live_expected_status",
    [
        # All 3 live routes MUST NOT 503. Status varies:
        #   pending → 200 (empty list)
        #   approve/deny → 404 (token "nope" not in queue)
        ("policy_get_pending", 200),
        ("policy_post_approve", 404),
        ("policy_post_deny", 404),
    ],
)
async def test_handler_selection(
    setup_name: str,
    expected_stub_status: int | None,
    route_name: str,
    live_expected_status: int,
    tmp_path,
    monkeypatch,
):
    """Stub vs live handler selection covers all 3 policy routes.

    The branch under test is ``build_settings_handlers``'s
    ``if not is_sidecar and approval_queue is not None`` — when that
    fails, ALL three live routes (pending/approve/deny) must fall back
    to stub handlers that 503. The old version tested only
    ``policy_get_pending``, so a regression that mis-wired approve or
    deny would have slipped through.
    """
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))

    server: object | None
    is_sidecar = False
    if setup_name == "sidecar":
        # Sidecar with a queue present STILL falls back to stubs — the
        # is_sidecar flag overrides queue presence.
        server = MagicMock()
        server.approval_queue = ApprovalQueue()
        is_sidecar = True
    elif setup_name == "no_server":
        server = None
    elif setup_name == "no_queue":
        # Main server but ENABLE_TOOL_SECURITY_POLICIES=false → queue is None
        server = MagicMock()
        server.approval_queue = None
    elif setup_name == "live":
        server = MagicMock()
        server.approval_queue = ApprovalQueue()
    else:  # pragma: no cover — parametrize guard
        raise AssertionError(f"unknown setup_name {setup_name!r}")

    handlers = build_settings_handlers(server, is_sidecar=is_sidecar)
    assert route_name in handlers

    request = (
        _fake_get_request()
        if route_name == "policy_get_pending"
        else _fake_post_request_with_token()
    )
    response = await handlers[route_name](request)
    expected = (
        live_expected_status if expected_stub_status is None else expected_stub_status
    )
    assert response.status_code == expected
