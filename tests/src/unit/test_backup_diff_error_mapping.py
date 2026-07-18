"""Dispatcher-level error-mapping tests for ``ha_manage_backup(action="diff")`` (#1950).

The ``(edits, diff)`` path maps exceptions raised by
``BackupManager.diff_snapshot`` onto tool-layer error codes:

    FileNotFoundError         -> RESOURCE_NOT_FOUND
    (ValueError, LookupError) -> VALIDATION_INVALID_PARAMETER
    ToolError                 -> re-raised unchanged (guard-pattern passthrough)
    any other Exception       -> exception_to_structured_error (INTERNAL_ERROR here)

Repo-wide this mapping had zero coverage: the unit suite drives
``BackupManager.diff_snapshot`` directly (below the dispatcher) and the e2e
suite exercises only the success path plus the pre-dispatch ``_require``
guard, so a reversed except-chain order or a changed error-code choice would
ship green. These tests drive the real ``ha_manage_backup`` tool closure with
``action="diff"`` and a ``diff_snapshot`` stub raising each class.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode

# A well-formed auto-backup name so the pre-dispatch ``_require`` guard passes
# and control reaches the ``diff_snapshot`` call under test.
_BNAME = "automation.kitchen_lights.20260521_153000.yaml"


def _dispatcher() -> Any:
    """Resolve the raw ``ha_manage_backup`` closure from the ``@tool`` stack.

    Mirrors the addon-tool tests: a fake ``mcp`` captures the decorated
    function, then ``__wrapped__`` is unwound past ``@log_tool_usage`` so the
    dispatcher is called directly, bypassing the FastMCP transport.
    """
    from ha_mcp.tools.backup import register_backup_tools

    captured: dict[str, Any] = {}

    class _MockMCP:
        def tool(self, *args: Any, **kwargs: Any) -> Any:
            def deco(fn: Any) -> Any:
                captured.setdefault(fn.__name__, fn)
                return fn

            return deco

    register_backup_tools(_MockMCP(), MagicMock())
    fn = captured["ha_manage_backup"]
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _patched(diff_exc: BaseException) -> tuple[Any, Any]:
    """Patch the ``scope="edits"`` dispatch seams.

    ``settings`` is irrelevant to the diff path (it is threaded through but
    unused by ``_edits_diff``), so a bare ``MagicMock`` suffices;
    ``get_backup_manager`` yields a manager whose ``diff_snapshot`` raises
    ``diff_exc``.
    """
    mgr = MagicMock()
    mgr.diff_snapshot = AsyncMock(side_effect=diff_exc)
    return (
        patch("ha_mcp.tools.backup.get_global_settings", return_value=MagicMock()),
        patch("ha_mcp.tools.backup.get_backup_manager", return_value=mgr),
    )


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        pytest.param(
            FileNotFoundError("gone"),
            ErrorCode.RESOURCE_NOT_FOUND,
            id="file_not_found_to_resource_not_found",
        ),
        pytest.param(
            ValueError("bad snapshot"),
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            id="value_error_to_validation",
        ),
        # LookupError is the second member of the ``(ValueError, LookupError)``
        # tuple - a separate case so narrowing the tuple to ``ValueError`` alone
        # cannot ship green.
        pytest.param(
            LookupError("missing key"),
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            id="lookup_error_to_validation",
        ),
        # A neutral message ("boom" matches no keyword bucket in
        # _classify_by_message) so the generic funnel lands on INTERNAL_ERROR
        # deterministically rather than a message-classified code.
        pytest.param(
            RuntimeError("boom"),
            ErrorCode.INTERNAL_ERROR,
            id="generic_to_structured_internal",
        ),
    ],
)
@pytest.mark.asyncio
async def test_diff_error_mapping(exc: BaseException, expected_code: ErrorCode) -> None:
    fn = _dispatcher()
    p1, p2 = _patched(exc)
    with p1, p2, pytest.raises(ToolError) as exc_info:
        await fn(scope="edits", action="diff", backup_name=_BNAME)
    assert expected_code.value in str(exc_info.value)


@pytest.mark.asyncio
async def test_diff_tool_error_passthrough_is_not_remapped() -> None:
    """A ``ToolError`` raised below the dispatcher must propagate unchanged -
    the ``except ToolError: raise`` guard must precede ``except Exception``,
    else the outer handler double-wraps it into INTERNAL_ERROR. Asserted by
    instance identity: a re-wrap would raise a NEW ``ToolError``."""
    sentinel = ToolError("sentinel-passthrough")
    fn = _dispatcher()
    p1, p2 = _patched(sentinel)
    with p1, p2, pytest.raises(ToolError) as exc_info:
        await fn(scope="edits", action="diff", backup_name=_BNAME)
    assert exc_info.value is sentinel
