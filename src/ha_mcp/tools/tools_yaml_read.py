"""Read-only YAML fragment lookup for Home Assistant config files.

The read companion to ``ha_config_set_yaml``: same ``file`` + ``yaml_path``
addressing, so a fragment found here can be handed straight back to the editor.

**Dependency:** Requires the ha_mcp_tools custom component (the round-trip parse
needs ``ruamel``, which the component carries and the MCP server does not).

Gating: behind ``enable_filesystem_tools``, NOT ENABLE_YAML_CONFIG_EDITING. The
editing flag gates *edits*, and reading a fragment is not one — but this returns
config-file contents through the same ``read_file``/``list_files`` component
services as ``ha_read_file``/``ha_list_files``, so it belongs behind the same
flag those are behind. Registering it unconditionally would hand an install that
deliberately turned filesystem tools off a config-file read surface through the
back door.
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
    is_filesystem_tools_enabled,
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


def _failure_text(payload: dict[str, Any]) -> str:
    """Readable reason out of a failure payload, for a per-file warning.

    The component answers with ``error`` as a plain string; the shared
    ``create_error_response`` shape nests a ``message`` under it instead.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or "read failed")


def _parse_error_result(
    target: str, yaml_path: str, parse_error: str, *, is_glob: bool
) -> tuple[None, str]:
    """Decide a file the component could not parse: raise for a single target.

    A single explicit target has no siblings to salvage, so a parse failure
    raises rather than soft-degrading to a warning that reads as "key absent".
    Under a glob it stays a per-file warning so one broken file does not sink
    the whole search.
    """
    if not is_glob:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"{target} could not be parsed as YAML: {parse_error}",
                suggestions=[f"Fix the YAML syntax error in {target} and retry."],
                context={"file": target, "yaml_path": yaml_path},
            )
        )
    return None, f"{target} was not searched: {parse_error}."


def _evaluate_read(
    response: Any,
    target: str,
    yaml_path: str,
    *,
    is_glob: bool,
    include_content: bool,
    include_parsed: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Turn one ``read_file`` response into a match, a warning, or neither.

    Three outcomes, in the order they are decided:

    * **warning** — the file could not be searched at all. Under a glob that
      must not sink the whole search and discard the matches already found:
      the glob is not restricted to ``*.yaml``, so ``packages/*`` can turn up
      a README the component refuses to read, and a permission-denied or
      non-UTF-8 ``.yaml`` lands here too. A syntactically broken file is the
      same class — reporting it as "key absent" would claim a file was
      inspected when it never was. A single explicit target has no siblings
      to salvage, so it raises instead.
    * **neither** — the file parsed and simply has no such key. The real
      non-match, and the whole point of a glob search.
    * **match** — the key is there.
    """
    # Only reachable under a glob: with a single target return_exceptions is
    # False, so gather itself raised.
    if isinstance(response, BaseException):
        return None, f"{target} was not searched: {response}."

    if not isinstance(response, dict):
        if is_glob:
            return None, f"{target} was not searched: unexpected read_file response."
        _call_failed("read_file", {"file": target, "yaml_path": yaml_path})
    unwrapped = unwrap_service_response(response)

    if not unwrapped.get("success", True):
        if not is_glob:
            raise_tool_error(unwrapped)
        return None, f"{target} was not searched: {_failure_text(unwrapped)}."

    parse_error = unwrapped.get("parse_error")
    if parse_error:
        return _parse_error_result(target, yaml_path, str(parse_error), is_glob=is_glob)

    if unwrapped.get("subtree") is None:
        return None, None

    match: dict[str, Any] = {"file": target, "yaml_path": yaml_path}
    if include_content:
        match["content"] = unwrapped["subtree"]
    if include_parsed:
        match["parsed"] = unwrapped.get("parsed")
    return match, None


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
        tags={"System", "beta"},
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
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
        allowlist stay unreadable, and secrets.yaml reads back with its values
        masked.
        """
        try:
            await _assert_mcp_tools_available(self._client)

            is_glob = _has_glob(file)
            targets = await _resolve_target_files(self._client, file)

            extra: dict[str, Any] = {"include_parsed": True} if include_parsed else {}
            # The reads are independent, so fan them out rather than paying N
            # sequential round-trips to HA — a packages glob is routinely 10+
            # files. gather preserves order, so matches stay sorted by file.
            # Under a glob a single read blowing up must not sink its siblings,
            # so exceptions come back as values to be warned about below.
            responses = await asyncio.gather(
                *(
                    call_mcp_tools_service(
                        self._client,
                        "read_file",
                        {"path": target, "yaml_path": yaml_path, **extra},
                    )
                    for target in targets
                ),
                return_exceptions=is_glob,
            )

            matches: list[dict[str, Any]] = []
            warnings: list[str] = []
            for target, response in zip(targets, responses, strict=True):
                match, warning = _evaluate_read(
                    response,
                    target,
                    yaml_path,
                    is_glob=is_glob,
                    include_content=include_content,
                    include_parsed=include_parsed,
                )
                if warning:
                    warnings.append(warning)
                elif match:
                    matches.append(match)

            result: dict[str, Any] = {
                "success": True,
                "yaml_path": yaml_path,
                "matches": matches,
                "count": len(matches),
                # Separates "the glob matched no files" from "no file defines
                # the key" — same empty matches[], different fix.
                "files_searched": len(targets),
            }
            if warnings:
                result["warnings"] = warnings
            return result

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
            # Unreachable (the call is typed NoReturn) but explicit, because
            # CodeQL cannot see the NoReturn and would otherwise read this
            # handler as falling through to an implicit None (py/mixed-returns).
            # The sibling tools additionally carry a `return None` AFTER the
            # try/except; that shape only works because their try ends in a
            # raise_tool_error() call CodeQL treats as returning. This try ends
            # in a real return, so the same trailer is provably dead there
            # (py/unreachable-statement).
            return None


def register_yaml_read_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the read-only YAML lookup tool.

    Behind ``enable_filesystem_tools`` (see module docstring): this returns
    config-file contents, so it registers with the same flag as the other
    file-read tools rather than with the YAML *editing* flag.
    """
    if not is_filesystem_tools_enabled():
        logger.debug("YAML read tool disabled (set HAMCP_ENABLE_FILESYSTEM_TOOLS=true)")
        return
    register_tool_methods(mcp, YamlReadTools(client))
