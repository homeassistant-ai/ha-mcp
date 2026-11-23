"""Enforce-mode guard for config-file reads and changes (``ha_read_file``,
``ha_write_file``, ``ha_delete_file``, ``ha_config_get_yaml``, and the file
backups of ``ha_manage_backup(scope="edits")``).

The component's ``read_file`` service applies ``tail_lines`` and ``yaml_path``
itself, and a slice can start anywhere inside a hidden block, so the decision is
made per file: while enforce mode hides a script, scene or automation, the call
is refused when a target file holds any part of a hidden item's definition.
Which file that is, ``!include`` and packages included, only Core's loader
knows, so the component answers from the configuration as Core loads it
(``ha_mcp_tools/config_file_items``). That costs one configuration load per
call, only in enforce mode; the component skips the load for its logs and
``secrets.yaml``.

The check asks about the file as Core loads it now, so a backup keeps the
content after the file stops defining the item: the file tools refuse before
their pre-write snapshot, and the backup tool refuses to read, compare,
restore or take a backup of a file while it defines a hidden item.
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
    client: Any, tool_name: str, paths: Iterable[str | None]
) -> None:
    """Refuse the call when any file in ``paths`` defines a hidden item, or
    when the component cannot tell. ``None`` stands for a file that cannot be
    named (a backup whose file is not known), and is refused."""
    from ..visibility.enforcement import (
        active_identifier_aliases,
        refuse_hidden_config_file,
    )

    aliases = await active_identifier_aliases(client)
    if aliases is None or not (aliases.script_services or aliases.storage_keys):
        return
    paths = list(paths)
    if None in paths:
        _refuse_unchecked(
            tool_name,
            ErrorCode.ENTITY_VISIBILITY_ENFORCED,
            "Which file the backup belongs to is not known.",
        )
    try:
        caps = await get_component_caps(client, strict=True)
    except ComponentDiscoveryError as exc:
        _refuse_unchecked(tool_name, ErrorCode.CONNECTION_FAILED, str(exc))
    if not component_supports(caps, _CAPABILITY):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "While enforce mode hides a script, scene or automation, reading, "
                "changing or backing up a config file needs a newer ha_mcp_tools "
                "component.",
                suggestions=[
                    "Update the HA-MCP integration through the Marketplace",
                    "Restart Home Assistant after the update completes",
                ],
                context={"tool": tool_name},
            )
        )
    items = await ask_component(client, _CAPABILITY, paths=paths)
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


async def refuse_changing_hidden_definitions(
    client: Any, tool_name: str, kwargs: dict[str, Any]
) -> None:
    """Refuse a write or delete of ``kwargs["path"]`` before its snapshot is
    taken (``with_auto_backup``'s ``precheck``). For ``edits=``, which edit
    failed would also tell whether a guessed text is in the file. A missing
    File & YAML Tools entry, which the user can fix, is reported first."""
    from .tools_filesystem import _assert_mcp_tools_available

    await _assert_mcp_tools_available(client)
    await refuse_reading_hidden_definitions(client, tool_name, [kwargs["path"]])


async def refuse_reading_hidden_backup(
    client: Any, domain: str, target: str | None
) -> None:
    """Refuse ``ha_manage_backup`` on a backup of a config file that defines a
    hidden item. ``file`` and ``yaml_file`` backups name the file; a ``yaml``
    backup's target is ``{file}::{yaml_path}``; all other domains hold no config
    file and pass. A target that names no file (empty, or a folder) is refused."""
    if domain == "yaml":
        target = (target or "").rpartition("::")[0]
    elif domain not in ("file", "yaml_file"):
        return
    if not target or target.endswith("/"):
        target = None
    await refuse_reading_hidden_definitions(client, "ha_manage_backup", [target])


def _refuse_unchecked(tool_name: str, code: ErrorCode, details: str | None) -> NoReturn:
    raise_tool_error(
        create_error_response(
            code,
            "Could not check whether the file defines a hidden script, scene or "
            "automation, so the call is refused.",
            details=details,
            context={"tool": tool_name},
        )
    )
