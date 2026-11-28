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

A configuration that does not load leaves every file unchecked. The calls that
can repair it (a whole-file write, a confirmed delete, a backup restore) then
run anyway, so the file tools can fix the error; the calls that show a file's
content, tell whether it holds a text (an edits= write) or back it up on
request stay refused.
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
_LOAD_ERROR_SUGGESTIONS = [
    "Fix the error in details: a whole-file ha_write_file, a confirmed "
    "ha_delete_file and a ha_manage_backup restore still run while the "
    "configuration does not load",
    "Or turn off enforce mode in the ha-mcp settings UI (Entity Visibility tab)",
]


async def refuse_reading_hidden_definitions(
    client: Any,
    tool_name: str,
    paths: Iterable[str | None],
    *,
    repairs: bool = False,
) -> None:
    """Refuse the call when any file in ``paths`` defines a hidden item, or
    when the component or the visibility config cannot tell. ``None`` stands
    for a file that cannot be named (a backup whose file is not known), and is
    refused. A call that ``repairs`` runs when the configuration does not load."""
    from ..visibility.enforcement import (
        active_identifier_aliases,
        refuse_hidden_config_file,
    )

    aliases = await active_identifier_aliases(client, tool_name)
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
        if repairs:
            return
        _refuse_unchecked(
            tool_name,
            ErrorCode.CONFIG_INVALID,
            items["error"],
            suggestions=_LOAD_ERROR_SUGGESTIONS,
        )
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
    failed would also tell whether a guessed text is in the file, so it does
    not count as a repair. A missing File & YAML Tools entry, which the user can
    fix, is reported first."""
    from .tools_filesystem import _assert_mcp_tools_available

    await _assert_mcp_tools_available(client)
    await refuse_reading_hidden_definitions(
        client, tool_name, [kwargs["path"]], repairs=kwargs.get("edits") is None
    )


async def refuse_reading_hidden_backup(
    client: Any, domain: str, target: str | None, *, repairs: bool = False
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
    await refuse_reading_hidden_definitions(
        client, "ha_manage_backup", [target], repairs=repairs
    )


def _refuse_unchecked(
    tool_name: str,
    code: ErrorCode,
    details: str | None,
    suggestions: list[str] | None = None,
) -> NoReturn:
    raise_tool_error(
        create_error_response(
            code,
            "Could not check whether the file defines a hidden script, scene or "
            "automation, so the call is refused.",
            details=details,
            suggestions=suggestions,
            context={"tool": tool_name},
        )
    )
