"""Extra YAML write keys: the operator's allowlist extension and its version gate.

Operators can extend the YAML write allowlist on the server and on the
component (#1887). The YAML tool and the backup restore path share these checks.
"""

import logging
from typing import Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error
from .tools_filesystem import (
    _COMPONENT_VERSION_CACHE,
    _ensure_caller_token,
    _version_tuple,
    call_mcp_tools_service,
)
from .util_helpers import unwrap_service_response

logger = logging.getLogger(__name__)

# Component service that returns the operator's component-side extra YAML write
# keys (#1887). Ships in the same component version as the ``extra_allowed_keys``
# field, so it is read only when the component is new enough (see
# ``effective_extra_yaml_write_keys``).
GET_EXTRA_YAML_KEYS_SERVICE = "get_extra_yaml_keys"

# First custom-component version whose ``edit_yaml_config`` schema accepts
# ``extra_allowed_keys`` (#1887).
MIN_COMPONENT_VERSION_EXTRA_YAML_KEYS = "1.2.4"


async def _component_supports_extra_keys(client: Any) -> bool:
    """Return whether the component is new enough for extra YAML write keys."""

    def _current() -> tuple[int, ...] | None:
        reported = _COMPONENT_VERSION_CACHE.get(client, "")
        if not reported:
            return None
        try:
            return _version_tuple(reported)
        except ValueError:
            return None

    floor = _version_tuple(MIN_COMPONENT_VERSION_EXTRA_YAML_KEYS)
    await _ensure_caller_token(client)
    parsed = _current()
    if parsed is None or parsed < floor:
        # The token cache is keyed by the long-lived REST client and survives
        # a Home Assistant restart, so a component updated after this process
        # first bootstrapped would keep reporting its old version forever and
        # the remediation the gate prints ("update, then restart HA") would
        # never take effect. Re-bootstrap once before answering no, so the
        # update heals on the next call rather than needing an ha-mcp restart.
        # Only on the failure path: a supported component costs nothing.
        await _ensure_caller_token(client, force_refresh=True)
        parsed = _current()
    return parsed is not None and parsed >= floor


async def assert_extra_yaml_keys_supported(client: Any, extra_keys: list[str]) -> None:
    """Block with an actionable prompt if the component predates #1887.

    ``edit_yaml_config``'s service schema is strict, so sending
    ``extra_allowed_keys`` to a component that does not declare the field makes
    Home Assistant reject the whole call with an opaque "extra keys not
    allowed". Callers only send the field when the operator configured keys,
    and this turns the remaining server-ahead-of-component window into a clear
    message. Both the write path and the backup restore path use it, so a
    snapshot taken under the setting fails the same recognisable way.

    Deliberately NOT a bump of ``MIN_COMPONENT_VERSION``: an operator who never
    sets extra keys must not be forced to update the component to keep using
    the filesystem tools.

    The version comes from the REST bootstrap cache, which every call to this
    component populates and validates, so a WebSocket hiccup cannot be
    mistaken for an outdated component. An absent entry means the bootstrap
    has not run yet; ``_component_supports_extra_keys`` performs it first
    (``_ensure_caller_token``), and a component too old to report a version at
    all is already rejected there.
    """
    if not extra_keys:
        return

    if await _component_supports_extra_keys(client):
        return
    reported = _COMPONENT_VERSION_CACHE.get(client, "")
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            "Extra YAML write keys are configured, but the installed "
            "ha_mcp_tools custom component does not support them "
            f"(reported version: {reported or 'unknown'}; requires >= "
            f"{MIN_COMPONENT_VERSION_EXTRA_YAML_KEYS}).",
            suggestions=[
                "Settings → System → Updates → HA-MCP → Update (HACS, or the Marketplace on Home Assistant 2026.11+)",
                "Restart Home Assistant after the update completes",
                # Parenthesised so this reads as one suggestion rather than
                # a list entry with a missing comma (py/implicit-string-
                # concatenation-in-list).
                (
                    "Or clear the extra YAML write keys setting to write "
                    "only the built-in allowed keys"
                ),
            ],
        )
    )


async def effective_extra_yaml_write_keys(client: Any, settings: Any) -> list[str]:
    """Return the write allowlist extension in force: server setting + component.

    The operator can set extra YAML write keys in two places (#1887): the ha-mcp
    server's own ``HA_MCP_EXTRA_YAML_KEYS`` and, via the integration UI, a store
    the component owns. A key set only on the component would otherwise be
    rejected by the server's own pre-dispatch allowlist before the write ever
    reaches the component, so the server reads that store here (via
    ``get_extra_yaml_keys``) and unions it with its own setting.

    That service ships in the same component version as the ``extra_allowed_keys``
    field (``MIN_COMPONENT_VERSION_EXTRA_YAML_KEYS``), so it is read only when the
    bootstrap reports a new-enough component; on an older component, or any read
    failure, fall back to the server setting alone and let
    ``assert_extra_yaml_keys_supported`` turn a real mismatch into an actionable
    prompt. The store is read per write rather than cached: it changes at runtime
    from the options flow, and a stale cache is the failure mode #1887's own
    version-cache heal exists to avoid. YAML writes are rare and human-driven, so
    the extra round-trip is negligible.
    """
    from ..config import parse_extra_yaml_write_keys

    keys: set[str] = set(parse_extra_yaml_write_keys(settings))
    try:
        if await _component_supports_extra_keys(client):
            result = await call_mcp_tools_service(
                client, GET_EXTRA_YAML_KEYS_SERVICE, {}
            )
            inner = (
                unwrap_service_response(result) if isinstance(result, dict) else None
            )
            if isinstance(inner, dict) and inner.get("success"):
                stored = inner.get("keys", [])
                if isinstance(stored, list):
                    keys.update(k for k in stored if isinstance(k, str) and k)
    except Exception:
        logger.debug(
            "Could not read the component extra-YAML-keys store; "
            "using the server setting alone.",
            exc_info=True,
        )
    return sorted(keys)
