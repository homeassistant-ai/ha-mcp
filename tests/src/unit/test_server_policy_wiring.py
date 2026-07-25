"""Test that ``_apply_tool_security_policies`` wires PolicyMiddleware correctly.

Mirrors the ``TestApplySearchKeywordEnrichment`` pattern in
``tests/src/unit/test_categorized_search.py``: build a ``MagicMock`` stub
exposing only the attributes the method touches and call the unbound
method directly. This avoids the full HA/FastMCP boot path in
``HomeAssistantSmartMCPServer.__init__`` (which would pull in the real
client, register every tool module, and run ``_initialize_server``).
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_server_stub(*, enable_policies: bool) -> MagicMock:
    """Minimal stub exposing only what ``_apply_tool_security_policies`` reads.

    The method touches:
      * ``self.settings.enable_tool_security_policies`` (early return gate)
      * ``self.approval_queue = ApprovalQueue()`` (attribute write)
      * ``self.mcp.add_middleware(...)`` (the wiring side-effect)
    """
    stub = MagicMock()
    stub.settings = MagicMock(enable_tool_security_policies=enable_policies)
    stub.mcp = MagicMock()
    # Explicitly start without the attribute the method is supposed to
    # set, so the disabled-case assertion is a real signal.
    del stub.approval_queue
    return stub


def test_policy_middleware_attached_when_enabled():
    """Enabled flag → ApprovalQueue attached and one PolicyMiddleware added."""
    from ha_mcp.policy.middleware import PolicyMiddleware
    from ha_mcp.server import HomeAssistantSmartMCPServer

    stub = _make_server_stub(enable_policies=True)
    HomeAssistantSmartMCPServer._apply_tool_security_policies(stub)

    # ApprovalQueue attached on the server object so settings_ui can find it
    assert hasattr(stub, "approval_queue")
    assert stub.approval_queue is not None

    # Middleware was wired in exactly once
    assert stub.mcp.add_middleware.call_count == 1
    args, _kwargs = stub.mcp.add_middleware.call_args
    assert len(args) == 1
    assert isinstance(args[0], PolicyMiddleware)
    # Queue identity: the middleware MUST hold the same ApprovalQueue
    # instance the server exposes via stub.approval_queue. If these
    # diverge, /api/policy/approve and the middleware's wait-loop look
    # at different queues and approvals silently never unblock the call.
    assert args[0]._queue is stub.approval_queue


def test_policy_middleware_not_attached_when_disabled():
    """Disabled flag → no queue, no middleware (clean no-op)."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    stub = _make_server_stub(enable_policies=False)
    HomeAssistantSmartMCPServer._apply_tool_security_policies(stub)

    # No queue attribute set (the early return runs before the assignment)
    assert getattr(stub, "approval_queue", None) is None
    # No middleware registered on the FastMCP instance
    assert stub.mcp.add_middleware.call_count == 0


def test_migration_runs_even_when_policies_disabled():
    """The ANY-match schema migration must run at startup regardless of the
    enable flag, so the file already matches the editor's semantics whenever
    the user later turns the feature on. The method imports
    ``migrate_policy_any_semantics`` from ``ha_mcp.policy.persistence`` at call
    time, so patching the source module intercepts it."""
    from unittest.mock import patch

    from ha_mcp.server import HomeAssistantSmartMCPServer
    from ha_mcp.utils.data_paths import get_data_dir

    stub = _make_server_stub(enable_policies=False)
    with patch(
        "ha_mcp.policy.persistence.migrate_policy_any_semantics"
    ) as mock_migrate:
        HomeAssistantSmartMCPServer._apply_tool_security_policies(stub)

    mock_migrate.assert_called_once_with(get_data_dir())
    # Disabled path is otherwise a clean no-op: no queue, no middleware.
    assert getattr(stub, "approval_queue", None) is None
    assert stub.mcp.add_middleware.call_count == 0


def test_raising_migration_does_not_block_startup():
    """A migration that raises must be swallowed — startup continues. A
    crash here would take down every server boot over a one-time data fixup."""
    from unittest.mock import patch

    from ha_mcp.server import HomeAssistantSmartMCPServer

    stub = _make_server_stub(enable_policies=False)
    with patch(
        "ha_mcp.policy.persistence.migrate_policy_any_semantics",
        side_effect=RuntimeError("migration boom"),
    ):
        # Must not propagate out of _apply_tool_security_policies.
        HomeAssistantSmartMCPServer._apply_tool_security_policies(stub)

    assert getattr(stub, "approval_queue", None) is None
    assert stub.mcp.add_middleware.call_count == 0
