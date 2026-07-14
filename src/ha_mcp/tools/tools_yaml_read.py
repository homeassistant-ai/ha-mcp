"""Read-only YAML fragment lookup for Home Assistant config files.

The read companion to ``ha_config_set_yaml``: same ``file`` + ``yaml_path``
addressing, so a fragment found here can be handed straight back to the editor.

**Dependency:** Requires the ha_mcp_tools custom component (the round-trip parse
needs ``ruamel``, which the component carries and the MCP server does not).

Unlike ``ha_config_set_yaml`` this is NOT behind ENABLE_YAML_CONFIG_EDITING:
reading a config fragment is not an edit, and an agent needs to inspect config
whether or not YAML editing is turned on.
"""

import asyncio
import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tools_filesystem import (
    _assert_mcp_tools_available,
    call_mcp_tools_service,
)
from .util_helpers import unwrap_service_response

logger = logging.getLogger(__name__)

# fnmatch metacharacters. Matching itself happens component-side (list_files
# fnmatches on the file name); this only decides single-file vs. glob.
_GLOB_METACHARS = ("*", "?", "[")


def _has_glob(file: str) -> bool:
    """True if ``file`` carries an fnmatch wildcard and must be expanded."""
    return any(char in file for char in _GLOB_METACHARS)


def _call_failed(service: str, context: dict[str, Any]) -> None:
    """Raise the shared 'unexpected response' error for a component service."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Unexpected response format from {service} service",
            context=context,
        )
    )


def _unwrap_or_raise(result: Any, service: str, context: dict[str, Any]) -> Any:
    """Unwrap a component service response, raising on a failure payload."""
    if not isinstance(result, dict):
        _call_failed(service, context)
    unwrapped = unwrap_service_response(result)
    if not unwrapped.get("success", True):
        raise_tool_error(unwrapped)
    return unwrapped


async def _resolve_target_files(client: Any, file: str) -> list[str]:
    """Expand ``file`` into the concrete config-relative paths to read.

    A plain path is returned as-is (the read itself reports a missing file). A
    glob is expanded through the component's ``list_files``, which matches the
    file name only — so ``packages/*.yaml`` covers one directory level, not a
    nested tree.
    """
    if not _has_glob(file):
        return [file]

    directory, _, pattern = file.rpartition("/")
    unwrapped = _unwrap_or_raise(
        await call_mcp_tools_service(
            client,
            "list_files",
            {"path": directory or ".", "pattern": pattern},
        ),
        "list_files",
        {"file": file},
    )
    entries = unwrapped.get("files")
    if not isinstance(entries, list):
        _call_failed("list_files", {"file": file})
    # Sorted so a multi-file result is deterministic across calls.
    return sorted(
        str(entry["path"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("path") and not entry.get("is_dir")
    )


class YamlReadTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_yaml",
        tags={"System"},
        annotations={
            "readOnlyHint": True,
            "title": "Read YAML Config Fragment",
        },
    )
    @log_tool_usage
    async def ha_config_get_yaml(
        self,
        yaml_path: Annotated[
            str,
            Field(
                description=(
                    "Dotted YAML key path to look up, e.g. 'alert2', 'mqtt', "
                    "'template'. Any key is readable — unlike ha_config_set_yaml, "
                    "which only writes an allowlisted set."
                ),
            ),
        ],
        file: Annotated[
            str,
            Field(
                default="configuration.yaml",
                description=(
                    "Config-relative file to read. Accepts an fnmatch glob to "
                    "search several files at once — 'packages/*.yaml' matches one "
                    "directory level, not a nested tree. Use the glob to find "
                    "which file defines a key."
                ),
            ),
        ] = "configuration.yaml",
        include_content: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Return the round-trip YAML text of each match. Set False to "
                    "discover only which files define the key."
                ),
            ),
        ] = True,
        include_parsed: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Additionally return each match as structured data. HA tags "
                    "stay in source form ('!secret api_key'), never resolved."
                ),
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Get the current YAML fragment under a key, from one config file or across a glob.

        Use before ha_config_set_yaml to inspect what a key currently holds, and
        to find which file defines it — the returned ``file`` + ``yaml_path`` are
        exactly the arguments that address the same fragment for an edit.

        Not for storage-mode items: automations, scripts and scenes created
        through the UI live outside YAML — read those with ha_config_get_automation,
        ha_config_get_script and ha_config_get_scene. This tool sees only what is
        written in the config files themselves.

        Reads the file, so it reflects what is on disk rather than what Home
        Assistant currently has loaded; a fragment edited but not yet reloaded
        differs from the running config. Comments and HA tags survive as written
        and a ``!secret`` is never resolved to its value, so ``content`` can be
        handed back to ha_config_set_yaml unchanged. Files outside the read
        allowlist and secrets.yaml itself stay unreadable.
        """
        try:
            await _assert_mcp_tools_available(self._client)

            targets = await _resolve_target_files(self._client, file)

            extra: dict[str, Any] = {"include_parsed": True} if include_parsed else {}
            # The reads are independent, so fan them out rather than paying N
            # sequential round-trips to HA — a packages glob is routinely 10+
            # files. gather preserves order, so matches stay sorted by file.
            responses = await asyncio.gather(
                *(
                    call_mcp_tools_service(
                        self._client,
                        "read_file",
                        {"path": target, "yaml_path": yaml_path, **extra},
                    )
                    for target in targets
                )
            )

            matches: list[dict[str, Any]] = []
            for target, response in zip(targets, responses, strict=True):
                unwrapped = _unwrap_or_raise(
                    response, "read_file", {"file": target, "yaml_path": yaml_path}
                )

                # None = the file parsed but has no such key: not an error, just
                # a non-match, which is the whole point of a glob search.
                if unwrapped.get("subtree") is None:
                    continue

                match: dict[str, Any] = {"file": target, "yaml_path": yaml_path}
                if include_content:
                    match["content"] = unwrapped["subtree"]
                if include_parsed:
                    match["parsed"] = unwrapped.get("parsed")
                matches.append(match)

            return {
                "success": True,
                "yaml_path": yaml_path,
                "matches": matches,
                "count": len(matches),
                # Separates "the glob matched no files" from "no file defines
                # the key" — same empty matches[], different fix.
                "files_searched": len(targets),
            }

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_get_yaml",
                    "file": file,
                    "yaml_path": yaml_path,
                },
            )
            return None  # raise_tool_error always raises; explicit for CodeQL
        return None  # py/mixed-returns: explicit terminal; handlers above always raise


def register_yaml_read_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the read-only YAML lookup tool.

    Intentionally unconditional: ENABLE_YAML_CONFIG_EDITING gates *editing*, and
    reading a fragment is useful (and safe) with editing switched off.
    """
    register_tool_methods(mcp, YamlReadTools(client))
