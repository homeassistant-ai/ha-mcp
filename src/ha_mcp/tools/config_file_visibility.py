"""Enforce-mode guard for config-file reads and edits (``ha_read_file``,
``ha_write_file(edits=)``, ``ha_config_get_yaml``).

The component's ``read_file`` service applies ``tail_lines`` and ``yaml_path``
itself, and a slice can start anywhere inside a hidden block, so the decision is
made per file: while enforce mode hides a script, scene or automation, the call
is refused when a target file holds any part of a hidden item's definition.
Which file that is, ``!include`` and packages included, only Core's loader
knows, so the component answers from the configuration as Core loads it
(``ha_mcp_tools/config_file_items``). That costs one configuration load per
call, only in enforce mode; the component skips the load for its logs and
``secrets.yaml``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NoReturn

from ..errors import ErrorCode, create_error_response
from .component_api import (
    ComponentDiscoveryError,
    ask_component,
    component_supports,
    get_component_caps,
)
from .helpers import raise_tool_error

_CAPABILITY = "config_file_items"


async def refuse_reading_hidden_definitions(
    client: Any, tool_name: str, paths: Iterable[str]
) -> None:
    """Refuse the call when any file in ``paths`` defines a hidden item, or
    when the component cannot tell."""
    from ..visibility.enforcement import (
        active_identifier_aliases,
        refuse_hidden_config_file,
    )

    aliases = await active_identifier_aliases(client)
    if aliases is None or not (aliases.script_services or aliases.storage_keys):
        return
    try:
        caps = await get_component_caps(client, strict=True)
    except ComponentDiscoveryError as exc:
        _refuse_unchecked(tool_name, ErrorCode.CONNECTION_FAILED, str(exc))
    if not component_supports(caps, _CAPABILITY):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "While enforce mode hides a script, scene or automation, reading "
                "or editing a config file needs a newer ha_mcp_tools component.",
                suggestions=[
                    "Update the HA-MCP integration through the Marketplace",
                    "Restart Home Assistant after the update completes",
                ],
                context={"tool": tool_name},
            )
        )
    items = await ask_component(client, _CAPABILITY, paths=list(paths))
    if items is None:
        _refuse_unchecked(tool_name, ErrorCode.SERVICE_CALL_FAILED, None)
    if "error" in items:
        _refuse_unchecked(tool_name, ErrorCode.CONFIG_INVALID, items["error"])
    scripts = aliases.script_services
    if any(key.casefold() in scripts for key in items["script"]) or any(
        config_id in aliases.storage_keys.get(domain, frozenset())
        for domain in ("automation", "scene")
        for config_id in items[domain]
    ):
        refuse_hidden_config_file(tool_name)


def _refuse_unchecked(tool_name: str, code: ErrorCode, details: str | None) -> NoReturn:
    raise_tool_error(
        create_error_response(
            code,
            "Could not check whether the file defines a hidden script, scene or "
            "automation, so it is neither read nor edited.",
            details=details,
            context={"tool": tool_name},
        )
    )
