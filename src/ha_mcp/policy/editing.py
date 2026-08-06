"""Shared read/write path for the tool-security-policy document.

Two MCP tools edit the same ``tool_policy.json``: the developer-mode
``ha_dev_manage_settings`` (``get_policy`` / ``set_policy``) and the
standalone ``ha_manage_security_policy`` (issue #2148). Both drive it
through these helpers so validation, the lock choreography, the
optimistic-concurrency check, the remember-cache invalidation, and the
response shapes cannot drift between the two surfaces.

Only the strings that name the calling tool's own actions vary, and
those come from the :class:`PolicyCaller` each tool passes in.
"""

from typing import Any, NamedTuple

from pydantic import ValidationError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error


class PolicyCaller(NamedTuple):
    """How the calling tool spells itself in user-facing messages."""

    tool: str
    get_action: str
    set_action: str

    @property
    def get_call(self) -> str:
        """The example call that reloads the policy, e.g. ``ha_x('get')``."""
        return f"{self.tool}('{self.get_action}')"


def _clear_remember_cache(server: Any | None) -> None:
    """Clear the approval remember-cache if a live queue exists."""
    queue = getattr(server, "approval_queue", None)
    if queue is not None:
        queue.clear_remember_cache()


def _with_file_lock(fn: Any, /, *args: Any) -> Any:
    """Run ``fn(*args)`` holding the cross-process config file lock.

    Thread-side companion of ``config_write_guard()``: callers hold the
    asyncio lock on the loop, so the file lock never nests in-process.
    """
    from ..utils.config_write_lock import config_file_lock

    with config_file_lock():
        return fn(*args)


async def get_policy(server: Any | None = None) -> dict[str, Any]:
    """Return the full tool-security policy plus its enforcement status."""
    from ..config import get_global_settings
    from ..utils.data_paths import get_data_dir
    from .persistence import load_policy

    try:
        policy = load_policy(get_data_dir())
    except ValueError as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"tool_policy.json is invalid: {exc}",
                suggestions=["Inspect or delete the file, then retry"],
            )
        )
    return {
        "success": True,
        "data": {
            "policy": policy.model_dump(mode="json"),
            "policies_enabled": (get_global_settings().enable_tool_security_policies),
            "policies_live": getattr(server, "approval_queue", None) is not None,
        },
    }


async def set_policy(
    policy: dict[str, Any] | None,
    expected_version: int | None,
    caller: PolicyCaller,
    server: Any | None = None,
) -> dict[str, Any]:
    """Write the full tool-security policy (validated, version-guarded)."""
    import asyncio

    from ..utils.config_write_lock import get_config_write_lock
    from .model import Policy

    if not isinstance(policy, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "'policy' (a policy object) is required for "
                f"action='{caller.set_action}'",
                suggestions=[f"Call {caller.get_call} for the shape"],
            )
        )
    try:
        new_policy = Policy.model_validate(policy)
    except (ValidationError, ValueError) as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"policy failed schema validation: {exc}",
            )
        )
    # Compare against the COERCED model version so a JSON string "3" matches
    # the on-disk int 3; ``"version" in policy`` distinguishes an omitted
    # version (no concurrency check) from an explicit one.
    expected = (
        expected_version
        if expected_version is not None
        else (new_policy.version if "version" in policy else None)
    )
    # Serialize the load-check-save against the web PUT handler + set_tool
    # (asyncio lock) and other processes (file lock, in the thread).
    async with get_config_write_lock():
        return await asyncio.to_thread(
            _with_file_lock, _commit_policy, new_policy, expected, caller, server
        )


def _commit_policy(
    new_policy: Any,
    expected: int | None,
    caller: PolicyCaller,
    server: Any | None,
) -> dict[str, Any]:
    """Load current policy, version-check against ``expected``, save, report.

    MUST run while holding ``get_config_write_lock()`` (called via
    ``asyncio.to_thread`` from :func:`set_policy`).
    """
    from ..config import get_global_settings
    from ..utils.data_paths import get_data_dir
    from .persistence import load_policy, save_policy

    data_dir = get_data_dir()
    try:
        current = load_policy(data_dir)
    except ValueError as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"existing tool_policy.json is invalid: {exc}",
                suggestions=["Inspect or delete the file, then retry"],
            )
        )
    warnings: list[str] = []
    if expected is not None and expected != current.version:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "policy version mismatch — reload with "
                f"{caller.get_action} before saving",
                context={
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                },
            )
        )
    if expected is None:
        warnings.append(
            "No version supplied; wrote without an optimistic-concurrency check."
        )
    # Rebase onto the on-disk version so save_policy bumps to current+1.
    save_policy(data_dir, new_policy.model_copy(update={"version": current.version}))
    rules_changed = current.rules != new_policy.rules
    if rules_changed:
        _clear_remember_cache(server)
    # Same "won't enforce" signal set_tool(gated=True) gives, so authoring
    # rules while the engine is off doesn't look like a live gate.
    if new_policy.rules and not get_global_settings().enable_tool_security_policies:
        warnings.append(
            "Tool security policies are disabled "
            "(enable_tool_security_policies=false); these rules are stored "
            "but won't enforce until policies are enabled and the server "
            "restarts."
        )
    result: dict[str, Any] = {
        "success": True,
        "data": {"version": current.version + 1, "rules_changed": rules_changed},
    }
    if warnings:
        result["warnings"] = warnings
    return result
