"""Tool-security-policy editing as a regular MCP tool (issue #2148).

Hidden behind the ``enable_security_policy_tool`` setting — the toggle on
the Tool Security Policies tab of the web settings UI, or the
``ENABLE_SECURITY_POLICY_TOOL`` env var. When the flag is off (the
default) ``register_security_policy_tools`` registers nothing, so the tool
does not exist for MCP clients at all.

The live approval QUEUE stays out of reach here by design: this tool reads
and writes the policy document only. Listing, approving, and denying
pending approvals remain developer-mode actions (``ha_dev_manage_server``).

Feature Flag: Set ENABLE_SECURITY_POLICY_TOOL=true to enable this tool.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..policy.editing import PolicyCaller, get_policy, set_policy
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)
from .util_helpers import JSON_STRING_COERCION

logger = logging.getLogger(__name__)

# Feature flag - disabled by default; the toggle lives on the Tool Security
# Policies tab of the web settings UI, under the master policies switch.
FEATURE_FLAG = "ENABLE_SECURITY_POLICY_TOOL"

# How the shared policy-editing helpers spell this tool's own actions when
# they build user-facing messages.
_POLICY_CALLER = PolicyCaller("ha_manage_security_policy", "get", "set")


class SecurityPolicyTools:
    """Read/write access to the tool security policy document."""

    def __init__(self, client: Any, server: Any) -> None:
        self._client = client
        # The live server object, passed by the registry. Used only to
        # report whether the approval engine is running (``policies_live``)
        # and to drop remembered approvals when the rules change — never to
        # reach the queue's contents.
        self._server = server

    @tool(
        name="ha_manage_security_policy",
        tags={"System"},
        annotations={
            "title": "Manage Security Policy",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    @log_tool_usage
    async def ha_manage_security_policy(
        self,
        action: Annotated[
            Literal["get", "set"],
            Field(
                description=(
                    "get: read the whole policy document. set: replace it "
                    "with the supplied one."
                )
            ),
        ],
        policy: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "set: the full policy object "
                    "{wait_seconds, approval_ttl_minutes, rules, version}"
                ),
            ),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "set: the version from your last get, for "
                    "optimistic-concurrency safety (else the policy's own "
                    "version field is used)"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage the tool security policy that gates high-stakes tool calls behind user approval.

        When NOT to use: this tool cannot see or decide pending approval
        requests — listing, approving, and denying them is developer-mode
        only (ha_dev_manage_server). For ordinary Home Assistant entity,
        automation, or dashboard work use the ha_config_* tools.

        When to use: to read the current policy, to add or remove per-tool
        approval rules and their matching conditions, and to change the
        approval wait time or how long an approval is remembered.

        Caveats: set replaces the WHOLE document, so send back an edited
        copy of what get returned, not a fragment. Writes are
        version-guarded: pass the version from the last get (or leave it in
        the policy body) and a concurrent edit is rejected instead of
        silently overwritten. Rule edits apply to the running server
        immediately and can remove approval gates. Whether this tool itself
        is registered is a separate setting that takes effect on restart.

        EXAMPLES:
        ha_manage_security_policy("get")
        ha_manage_security_policy("set", policy={"wait_seconds": 60, "approval_ttl_minutes": 5, "rules": [{"tool_name": "ha_call_service"}], "version": 3})
        """
        try:
            if action == "get":
                return await get_policy(self._server)
            return await set_policy(
                policy, expected_version, _POLICY_CALLER, self._server
            )
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action},
                suggestions=["Check server logs for details"],
            )
            return None  # unreachable; explicit for CodeQL


def register_security_policy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the security-policy tool when the feature flag is on.

    Registers NOTHING unless ``enable_security_policy_tool`` is set — the
    tool is invisible to MCP clients by default. Mirrors the early-return
    gate used by register_dashboard_screenshot_tools.
    """
    from ..config import get_global_settings

    # ``getattr`` with a False default, not attribute access: during an
    # in-process package update the cached settings singleton can predate
    # this field (issues #1783/#1785), and that stale read must mean "tool
    # off", never AttributeError.
    if not getattr(get_global_settings(), "enable_security_policy_tool", False):
        logger.debug(f"Security policy tool disabled (set {FEATURE_FLAG}=true)")
        return

    logger.warning(
        "ha_manage_security_policy is registered: connected MCP clients can "
        "read and rewrite the tool security policies, including removing "
        "approval gates."
    )
    # kwargs["server"], not .get(): the registry always passes it, and a
    # KeyError from a future call site that forgets is caught and logged
    # loudly by the registry — better than silently registering a tool that
    # cannot report whether the approval engine is live.
    register_tool_methods(mcp, SecurityPolicyTools(client, server=kwargs["server"]))
