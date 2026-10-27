"""Enforce-mode guard for config-file reads (``ha_read_file``, ``ha_config_get_yaml``).

The component's ``read_file`` service applies ``tail_lines`` and ``yaml_path``
itself, and a slice can start anywhere inside a hidden block, so the decision is
made on the whole file: while enforce mode hides a script, scene or automation,
each YAML target is read in full first and the call is refused when that file
defines a hidden item. That costs one extra read per file, only in enforce mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from ..visibility.config_files import defines_hidden_item, is_yaml_path
from .tools_filesystem import call_mcp_tools_service
from .util_helpers import unwrap_service_response


async def refuse_reading_hidden_definitions(
    client: Any, tool_name: str, paths: Iterable[str]
) -> None:
    """Refuse the call when any YAML file in ``paths`` defines a hidden item.

    A file the full read cannot return is left to the real read, which reports
    the same failure. A transport error propagates rather than letting the
    sliced read run unchecked.
    """
    from ..visibility.enforcement import (
        active_identifier_aliases,
        refuse_hidden_config_file,
    )

    aliases = await active_identifier_aliases(client)
    if aliases is None or not (aliases.script_services or aliases.storage_keys):
        return
    targets = [path for path in paths if is_yaml_path(path)]
    responses = await asyncio.gather(
        *(
            call_mcp_tools_service(client, "read_file", {"path": path})
            for path in targets
        )
    )
    for path, response in zip(targets, responses, strict=True):
        body = unwrap_service_response(response) if isinstance(response, dict) else {}
        content = body.get("content")
        # Off the event loop: the embedded server shares Home Assistant's loop,
        # and a large file's pure-Python parse would stall it.
        if isinstance(content, str) and await asyncio.to_thread(
            defines_hidden_item, path, content, aliases
        ):
            refuse_hidden_config_file(tool_name)
