"""Extra YAML write keys: the operator's allowlist extension.

Operators can extend the YAML write allowlist on the server and on the
component (#1887). The YAML tool and the backup restore path share this read.
"""

import logging
from typing import Any

from .tools_filesystem import call_mcp_tools_service
from .util_helpers import unwrap_service_response

logger = logging.getLogger(__name__)

# Component service that returns the operator's component-side extra YAML write
# keys (#1887).
GET_EXTRA_YAML_KEYS_SERVICE = "get_extra_yaml_keys"


async def effective_extra_yaml_write_keys(client: Any, settings: Any) -> list[str]:
    """Return the write allowlist extension in force: server setting + component.

    The operator can set extra YAML write keys in two places (#1887): the ha-mcp
    server's own ``HA_MCP_EXTRA_YAML_KEYS`` and, via the integration UI, a store
    the component owns. A key set only on the component would otherwise be
    rejected by the server's own pre-dispatch allowlist before the write ever
    reaches the component, so the server reads that store here (via
    ``get_extra_yaml_keys``) and unions it with its own setting.

    On any read failure, fall back to the server setting alone; a component
    older than this release is refused by the write itself
    (``MIN_COMPONENT_VERSION``). The store is read per write rather than
    cached: it changes at runtime from the options flow. YAML writes are rare
    and human-driven, so the extra round-trip is negligible.
    """
    from ..config import parse_extra_yaml_write_keys

    keys: set[str] = set(parse_extra_yaml_write_keys(settings))
    try:
        result = await call_mcp_tools_service(client, GET_EXTRA_YAML_KEYS_SERVICE, {})
        inner = unwrap_service_response(result) if isinstance(result, dict) else None
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
