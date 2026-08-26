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

import logging
from typing import Any, NamedTuple

from pydantic import ValidationError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error

logger = logging.getLogger(__name__)


class PolicyCaller(NamedTuple):
    """How the calling tool spells itself in user-facing messages."""

    tool: str
    get_action: str
    set_action: str

    @property
    def get_call(self) -> str:
        """The example call that reads the current policy, e.g. ``ha_x('get')``."""
        return f"{self.tool}('{self.get_action}')"


def _clear_remember_cache(server: Any | None) -> None:
    """Clear the approval remember-cache after a rule change.

    Called only when the rules actually changed, so a missing queue is
    worth a WARNING: the rules the operator just wrote are not what the
    engine (wherever it runs) is still remembering.
    """
    queue = getattr(server, "approval_queue", None)
    if queue is None:
        logger.warning(
            "Policy rules changed but no approval queue is live in this "
            "process; remembered approvals were not cleared."
        )
        return
    queue.clear_remember_cache()


async def get_policy(server: Any) -> dict[str, Any]:
    """Return the full tool-security policy plus its enforcement status."""
    import asyncio

    from ..config import get_global_settings
    from ..utils.data_paths import get_data_dir
    from .persistence import load_policy

    try:
        policy = await asyncio.to_thread(load_policy, get_data_dir())
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
    server: Any,
) -> dict[str, Any]:
    """Write the full tool-security policy (validated, version-guarded)."""
    import asyncio

    from ..utils.config_write_lock import get_config_write_lock, run_with_file_lock
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
    # ``rules`` defaults to [] on the model and Policy ignores unknown keys,
    # so an omitted (or misspelled) key would validate and silently delete
    # every approval gate. Demand it explicitly instead.
    if "rules" not in policy:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "'rules' is missing from the policy document. "
                f"action='{caller.set_action}' replaces the WHOLE document, so "
                "an omitted 'rules' would delete every approval gate. Send "
                '"rules": [] to clear them deliberately.',
                suggestions=[f"Call {caller.get_call} and resend the edited document"],
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
            run_with_file_lock, _commit_policy, new_policy, expected, caller, server
        )


def _pre_save_warnings(
    new_policy: Any, current: Any, expected: int | None, server: Any
) -> list[str]:
    """Every warning the response carries that does not depend on the write.

    Computed BEFORE ``save_policy`` runs: these read the Settings singleton
    and the server object, and an exception from either after a committed
    write would surface as INTERNAL_ERROR on a save that actually landed —
    the caller would then retry and hit a spurious version mismatch.
    """
    from ..config import get_global_settings

    warnings: list[str] = []
    if expected is None:
        warnings.append(
            "No version supplied; wrote without an optimistic-concurrency check."
        )
    removed = [rule for rule in current.rules if rule not in new_policy.rules]
    if removed:
        names = ", ".join(sorted({rule.tool_name for rule in removed}))
        warnings.append(
            f"This write removed {len(removed)} existing rule(s), covering: "
            f"{names}. Resend them if that was not intended."
        )
    if not new_policy.rules:
        return warnings
    policies_enabled = get_global_settings().enable_tool_security_policies
    if not policies_enabled:
        # Same "won't enforce" signal set_tool(gated=True) gives, so authoring
        # rules while the engine is off doesn't look like a live gate.
        warnings.append(
            "Tool security policies are disabled "
            "(enable_tool_security_policies=false); these rules are stored "
            "but won't enforce until policies are enabled and the server "
            "restarts."
        )
    elif getattr(server, "approval_queue", None) is None:
        # Flag on but no queue: the enabled-but-not-yet-live window (a
        # restart is pending) or the middleware failed to load. Either way
        # nothing is gated right now, which plain success would hide.
        warnings.append(
            "The approval engine is NOT running in this process (a restart is "
            "pending, or the policy middleware failed to load — check the "
            "server log); these rules are stored but nothing is gated until "
            "it is live."
        )
    return warnings


def _commit_policy(
    new_policy: Any,
    expected: int | None,
    caller: PolicyCaller,
    server: Any,
) -> dict[str, Any]:
    """Load current policy, version-check against ``expected``, save, report.

    MUST run while holding ``get_config_write_lock()`` (called via
    ``asyncio.to_thread`` from :func:`set_policy`).
    """
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
    if expected is not None and expected != current.version:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "policy version mismatch — reload with "
                f"{caller.get_call} before saving",
                context={
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                },
            )
        )
    rules_changed = current.rules != new_policy.rules
    warnings = _pre_save_warnings(new_policy, current, expected, server)
    # Rebase onto the on-disk version so save_policy bumps to current+1.
    save_policy(data_dir, new_policy.model_copy(update={"version": current.version}))
    if rules_changed:
        # The write has landed. Nothing after this point may turn a committed
        # save into a failure the caller would retry.
        try:
            _clear_remember_cache(server)
        except Exception:
            logger.warning(
                "policy saved but remember-cache clear failed", exc_info=True
            )
            warnings.append(
                "The policy was saved, but remembered approvals could not be "
                "cleared; an earlier approval may still be honored until its "
                "TTL expires."
            )
    result: dict[str, Any] = {
        "success": True,
        "data": {"version": current.version + 1, "rules_changed": rules_changed},
    }
    if warnings:
        result["warnings"] = warnings
    return result
