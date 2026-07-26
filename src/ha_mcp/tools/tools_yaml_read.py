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
import re
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

# The metacharacters that match their own literal names, and so are pattern
# syntax rather than a plausible spelling of a file name the caller means
# exactly. A bracket class is the ambiguous one and is deliberately absent.
_PATTERN_ONLY_METACHARS = ("*", "?")

# Wraps each metacharacter in a bracket class so fnmatch reads it literally.
# Deliberately not glob.escape: it splits a drive off first and returns that
# part UNESCAPED, and the split is ntpath on a Windows-hosted server - where a
# name like \\weird[a].yaml is taken for a drive in its entirety and comes back
# with its bracket intact. The one host-dependent step in a path that is
# otherwise POSIX and config-relative.
_METACHAR_RE = re.compile(r"([*?\[])")


def _has_glob(name: str) -> bool:
    """True if ``name`` carries an fnmatch wildcard and must be expanded.

    Takes the file name rather than the whole path: the component resolves
    directories literally, so a metacharacter in a folder name belongs to that
    folder's name and is not a pattern.
    """
    return any(char in name for char in _GLOB_METACHARS)


def _names_a_file(pattern: str) -> bool:
    """True if the request can be read as naming one file rather than a class.

    ``*`` and ``?`` are pattern syntax: they match their own literal names, so
    a caller who wrote one wrote a pattern. A bracket class alone is ambiguous,
    since such a name is legal on the filesystem and cannot match itself.

    One predicate for two decisions that must not diverge - whether a path the
    literal lookup returned counts as explicitly named, and whether the absence
    of such a path is worth a warning. Honouring it in the warning alone would
    leave a file genuinely called ``[ab]*.yaml`` strict, so one failed read of
    it would discard every match the pattern half had already found: the shape
    removed for ``*.yaml`` when provenance moved into the resolver, reachable
    again through the other lookup.
    """
    return not any(char in pattern for char in _PATTERN_ONLY_METACHARS)


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
    target: str, yaml_path: str, parse_error: str, *, is_expanded: bool
) -> tuple[None, str]:
    """Decide a file the component could not parse: raise for a named target.

    The explicitly named target raises rather than soft-degrading to a warning
    that reads as "key absent" - it raises even when expansion turned up
    siblings alongside it, discarding their results, because a named file that
    cannot be parsed is the caller's question, not a stray unreadable neighbour.
    An expanded target stays a per-file warning so one broken file does not
    sink the whole search.
    """
    if not is_expanded:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"{target} could not be parsed as YAML: {parse_error}",
                suggestions=[f"Fix the YAML syntax error in {target} and retry."],
                context={"file": target, "yaml_path": yaml_path},
            )
        )
    return None, f"{target} was not searched: {parse_error}."


def _read_exception_result(
    target: str, error: BaseException, *, is_expanded: bool
) -> tuple[None, str]:
    """Decide a read that blew up outright.

    An expanded target degrades to a warning so one unreadable file cannot sink
    the search; the explicitly named target raises, as it does for every other
    failure class in :func:`_evaluate_read`. The error is re-raised as it came
    rather than wrapped, so the tool's own handler classifies it by type - the
    other classes have no live exception to preserve and build a structured
    :class:`ToolError` instead.

    Anything that is not an ``Exception`` propagates whatever the target's
    provenance - this is the one failure class here that raises for an expanded
    target too. Cancellation and shutdown are not one file being unreadable,
    and absorbing them into a warning next to ``success: true`` would report a
    search that was called off as one that ran and found nothing. The per-item
    fan-outs in ``tools_entities`` and ``tools_search`` draw the same line.

    ``str()`` is empty on a bare ``TimeoutError()``, so the class name stands
    in - a warning reading "was not searched: ." names no reason at all.
    """
    if not is_expanded or not isinstance(error, Exception):
        raise error
    return None, f"{target} was not searched: {str(error) or type(error).__name__}."


def _evaluate_read(
    response: Any,
    target: str,
    yaml_path: str,
    *,
    is_expanded: bool,
    include_content: bool,
    include_parsed: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Turn one ``read_file`` response into a match, a warning, or neither.

    Three outcomes, in the order they are decided:

    * **warning** — the file could not be searched at all. An expanded target
      must not sink the whole search and discard the matches already found:
      expansion is not restricted to ``*.yaml``, so ``packages/*`` can turn up
      a README the component refuses to read, and a permission-denied or
      non-UTF-8 ``.yaml`` lands here too. A syntactically broken file is the
      same class — reporting it as "key absent" would claim a file was
      inspected when it never was. The explicitly named target raises
      instead, even when expansion turned up siblings alongside it.
    * **neither** — the file parsed and simply has no such key. The real
      non-match, and the whole point of a glob search.
    * **match** — the key is there.
    """
    if isinstance(response, BaseException):
        return _read_exception_result(target, response, is_expanded=is_expanded)

    if not isinstance(response, dict):
        if is_expanded:
            return None, f"{target} was not searched: unexpected read_file response."
        _call_failed("read_file", {"file": target, "yaml_path": yaml_path})
    unwrapped = unwrap_service_response(response)

    if not unwrapped.get("success", True):
        if not is_expanded:
            raise_tool_error(unwrapped)
        return None, f"{target} was not searched: {_failure_text(unwrapped)}."

    parse_error = unwrapped.get("parse_error")
    if parse_error:
        return _parse_error_result(
            target, yaml_path, str(parse_error), is_expanded=is_expanded
        )

    if unwrapped.get("subtree") is None:
        return None, None

    match: dict[str, Any] = {"file": target, "yaml_path": yaml_path}
    if include_content:
        match["content"] = unwrapped["subtree"]
    if include_parsed:
        match["parsed"] = unwrapped.get("parsed")
    return match, None


async def _list_files_matching(
    client: Any, file: str, directory: str, pattern: str
) -> list[str]:
    """Return the non-directory paths in ``directory`` matching ``pattern``."""
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


def _absent_literal_warnings(
    file: str, pattern: str, *, literal_found: bool, target_count: int
) -> list[str]:
    """Say so when nothing is literally named the way a bracketed request was.

    Silence is what the caller reads as "your file was searched": the siblings
    the class matched arrive as ``files_searched`` with nothing marking the
    named file as never opened.

    Two cases stay silent. A literal that was found needs no warning, and
    neither does a request that :func:`_names_a_file` rejects - nobody named a
    file, so nothing is missing. The literal lookup still runs for those, since
    its results are merged as ordinary expansion matches; only the complaint is
    dropped.
    """
    if literal_found or not _names_a_file(pattern):
        return []
    if not target_count:
        # Phrased for the degenerate case rather than reporting "the 0 file(s)
        # matching it as a pattern", which reads as a search that happened.
        return [
            f"No file is literally named {file}, and nothing matched it as a "
            f"pattern either."
        ]
    return [
        f"No file is literally named {file}; searched the {target_count} "
        f"file(s) matching it as a pattern."
    ]


async def _resolve_target_files(
    client: Any, file: str
) -> tuple[list[tuple[str, bool]], list[str]]:
    """Expand ``file`` into ``(path, is_expanded)`` pairs plus any warnings.

    ``is_expanded`` records where the path came from, and it is decided here
    because only here is it known: a path produced by the pattern lookup is an
    expansion, while the requested path itself, and what the escaped-literal
    lookup returns for a request :func:`_names_a_file` accepts, were named
    explicitly. Recovering that downstream by comparing the path against the
    requested string would be wrong for ``*`` and ``?``, which match their own
    literal names - a file genuinely called ``*.yaml`` would then be mistaken
    for the name the caller typed and its read failure would sink the whole
    search.

    A plain path is returned as-is (the read itself reports a missing file). A
    glob is expanded through the component's ``list_files``, which matches the
    file name only, so ``packages/*.yaml`` covers one directory level rather
    than a nested tree. Only the name is inspected for metacharacters: the
    component resolves the directory literally, so ``pack[a]ges/svc.yaml``
    names one file and is read as one rather than expanded.

    A bracketed name is ambiguous: ``packages/svc[a].yaml`` is both a valid
    pattern and a valid filename, and a closed bracket class is the one
    metacharacter form whose pattern cannot match its own name (``fnmatch``
    reads ``[a]`` as the single character ``a``, while ``*`` and ``?`` do match
    themselves). Such a name is therefore looked up twice, as the pattern and
    as the escaped literal, and the results are merged: the pattern alone would
    report ``files_searched: 0`` for a file that exists, and the literal alone
    would drop the siblings a deliberate class like ``svc[12].yaml`` is meant
    to find. Both lookups run because a sibling that satisfies the class would
    otherwise mask the file the caller named exactly. Only bracketed names pay
    the second lookup, and it is issued concurrently with the first rather than
    after it. The component likewise treats package folder names carrying
    metacharacters as literal names (#1854).

    The guard is any bracket, not a closed class: an unclosed or empty bracket
    is literal to ``fnmatch`` and so matches its own name, which means both
    lookups return it and the deduplication below is what keeps it a single
    target. A listing failure on either lookup raises rather than degrading -
    the directory is the same one in both calls, so a failure there is a broken
    request, not one unreadable file among several.

    When the literal lookup comes back empty the caller named a file that does
    not exist, and the pattern's siblings would otherwise be reported as if the
    named file had been searched; :func:`_absent_literal_warnings` decides what
    to say about that.
    """
    directory, _, pattern = file.rpartition("/")
    if not _has_glob(pattern):
        return [(file, False)], []

    if "[" not in pattern:
        matched = await _list_files_matching(client, file, directory, pattern)
        return [(target, True) for target in matched], []

    # The two lookups are independent, so run them together rather than paying
    # two sequential round-trips; the reads below fan out for the same reason.
    matched, literal = await asyncio.gather(
        _list_files_matching(client, file, directory, pattern),
        _list_files_matching(
            client, file, directory, _METACHAR_RE.sub(r"[\1]", pattern)
        ),
    )
    # Both lookups always contribute targets; only whether a literal hit counts
    # as *named* depends on the request. Under a pattern it is merged as an
    # ordinary expansion match, so nobody named it and a bad read stays a
    # warning.
    literal_paths = set(literal)
    named = literal_paths if _names_a_file(pattern) else set()
    targets = sorted(set(matched) | literal_paths)
    warnings = _absent_literal_warnings(
        file, pattern, literal_found=bool(named), target_count=len(targets)
    )
    return [(target, target not in named) for target in targets], warnings


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

            # Strictness is a property of each target, not of the request, so
            # the resolver reports which targets were named rather than expanded.
            targets, resolve_warnings = await _resolve_target_files(self._client, file)

            extra: dict[str, Any] = {"include_parsed": True} if include_parsed else {}
            # The reads are independent, so fan them out rather than paying N
            # sequential round-trips to HA — a packages glob is routinely 10+
            # files. gather preserves order, so matches stay sorted by file.
            # An expanded target blowing up must not sink the search, so every
            # exception comes back as a value; whether it degrades to a warning
            # or is re-raised is then decided per target, like every other
            # failure class.
            responses = await asyncio.gather(
                *(
                    call_mcp_tools_service(
                        self._client,
                        "read_file",
                        {"path": target, "yaml_path": yaml_path, **extra},
                    )
                    for target, _ in targets
                ),
                return_exceptions=True,
            )

            matches: list[dict[str, Any]] = []
            warnings: list[str] = list(resolve_warnings)
            files_unreadable = 0
            for (target, is_expanded), response in zip(targets, responses, strict=True):
                match, warning = _evaluate_read(
                    response,
                    target,
                    yaml_path,
                    is_expanded=is_expanded,
                    include_content=include_content,
                    include_parsed=include_parsed,
                )
                if warning:
                    warnings.append(warning)
                    files_unreadable += 1
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
                # Keeps `count: 0` beside a non-zero files_searched from reading
                # as "searched them, found nothing". Counted in the loop, not as
                # len(warnings): those also carry resolver-level entries.
                "files_unreadable": files_unreadable,
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
