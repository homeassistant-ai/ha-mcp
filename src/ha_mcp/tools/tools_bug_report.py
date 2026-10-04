"""
Bug report tool for Home Assistant MCP Server.

This module provides a tool to collect diagnostic information and guide users
on how to create effective bug reports.
"""

import asyncio
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

import httpx
from pydantic import Field

from ha_mcp import __version__
from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.server.dependencies import get_http_headers
from ha_mcp._vendor.fastmcp.tools import tool

from .._version import get_version, is_embedded, is_running_in_addon
from ..client.supervisor_client import make_supervisor_httpx_client
from ..config import Settings, get_global_settings
from ..errors import create_validation_error
from ..utils.mcp_client_host import detect_client_host
from ..utils.usage_logger import (
    AVG_LOG_ENTRIES_PER_TOOL,
    get_recent_logs,
    get_startup_logs,
)
from .bug_report_config import collect_config_toggles
from .bug_report_templates import (
    REPORT_END_MARKER,
    _build_issue_url,
    _format_client_host_for_template,
    _format_client_info_for_template,
    _format_server_entry_value,
    _format_supervisor_value,
    _format_tools_entry_value,
    _format_version_value,
    _generate_agent_behavior_template,
    _generate_feature_request_template,
    _generate_runtime_bug_template,
    _generate_search_keywords,
    _ReportText,
    _sanitize_log_text,
    _suggested_title,
)
from .coercion import ANSI_ESCAPE_RE, JSON_STRING_COERCION, parse_string_list_param
from .component_api import get_component_caps
from .helpers import (
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .response_helpers import project_fields
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

# Report types whose body carries the tool-call log, environment and
# configuration, but no startup, app or Core error logs.
_AGENT_SIDE_TEMPLATES = {
    "agent_behavior": _generate_agent_behavior_template,
    "feature_request": _generate_feature_request_template,
}


_TITLE_PREFIXES = {
    "runtime_bug": "[BUG] ",
    "agent_behavior": "[AGENT] ",
    "feature_request": "[FEATURE] ",
}

# GitHub refuses a longer issue title.
_ISSUE_TITLE_MAX_CHARS = 256

# Guidance surfaced when the reported problem is a tool that never shows up in
# the client's tool list. In issue #1804 this produced a false bug report: the
# tool was enabled and loaded server-side (startup logs even said so), but the
# MCP client had never refreshed its cached tool list, so the agent concluded
# it was a server bug. A stale client-side tool list is the single most likely
# cause of a missing tool and must be ruled out before filing.
MISSING_TOOL_HINT = (
    "MISSING/UNAVAILABLE TOOL? If the problem is that a tool you expected is not "
    "in your tool list (can't find it, can't call it), this is very likely NOT a "
    "bug. MCP clients cache the tool list when they connect and do NOT pick up "
    "newly enabled tools until the connection is refreshed. Server-side logs "
    "saying a tool is 'enabled' do NOT mean the client has loaded it. Ask the "
    "user to refresh their MCP connection first: in Claude Desktop / claude.ai "
    "use the connector's refresh option, or simply disconnect and reconnect it; "
    "in Claude Code run /mcp and reconnect the server. Only file a bug if the "
    "tool is still missing after a refresh."
)

# Known Claude Desktop bugs that present as ha-mcp failures. Both live in
# the client, never reach this server, and already have upstream tickets, so
# the agent must rule them out before filing here.
KNOWN_CLIENT_ISSUES_HINT = (
    "KNOWN CLAUDE DESKTOP ISSUES (not ha-mcp bugs; check before filing):\n"
    "1. A write tool (dashboard, automation, script, helper, service call) "
    "hangs and the client reports a 4-minute timeout with no result, while "
    "read tools work. Cause: Claude Desktop's manual-approval dialog for a "
    "local MCP server accepts the click before the call is fully generated "
    "and silently drops the call. The server never receives it. Tracked in "
    "homeassistant-ai/ha-mcp#2367 and anthropics/claude-code#92014. Tell the "
    "user to retry and wait a few seconds before clicking Allow once, or to "
    "set the write tools to Always allow (Settings -> Connectors -> the "
    "server -> Tool permissions). A dropped call never reached Home "
    "Assistant, but the same timeout can also hide a call that landed and "
    "lost only its result (anthropics/claude-code#80012), so read the target "
    "back before repeating a write that is not idempotent.\n"
    "2. Every tool call fails with 'expected nonoptional, received "
    "undefined' when an optional parameter is omitted. Cause: Claude Desktop "
    "2.110.0 rejects omitted optional MCP parameters. Tracked in "
    "homeassistant-ai/ha-mcp#2472 and anthropics/claude-code#94608 (fixed "
    "upstream). Tell the user to update Claude Desktop to 2.2553.0 or later; "
    "if they cannot update, downgrading to 1.52386.6 (links in #2472) also "
    "works.\n"
    "Only file a bug if the problem persists after the matching workaround."
)

# Max characters to include from addon container logs.
# 3000 chars ≈ 750 LLM tokens — keeps the tool response well below context budgets
# while still capturing enough recent output to diagnose most issues.
_ADDON_LOG_MAX_CHARS = 3000

# Max characters to include from the Home Assistant error log (home-assistant.log).
# Slightly larger than the addon cap: this log carries the highest-value
# diagnostic lines (auth failures, integration tracebacks) and the recent tail
# is where they land. ~5000 chars ≈ 1250 LLM tokens.
_CORE_LOG_MAX_CHARS = 5000

# Log window requested from the install for that tail. Only the last
# _CORE_LOG_MAX_CHARS survive into the report — a few dozen lines of typical
# HA log text — so anything deeper is fetched and then thrown away, which on
# Supervisor-backed installs is exactly the cost that made the unconditional
# 20,000-line fetch hang (#2279). 500 keeps an order of magnitude of headroom
# for short lines.
_CORE_LOG_WINDOW_LINES = 500

# A slow or missing Supervisor must not hold up the whole report.
_SUPERVISOR_PROBE_TIMEOUT = 10.0


def _detect_installation_method() -> str:
    """
    Detect how ha-mcp was installed.

    Returns one of: pyinstaller, embedded, addon, docker, git, pypi, unknown
    """
    # 1. PyInstaller binary
    if getattr(sys, "frozen", False):
        return "pyinstaller"

    # 2. In-process server inside HA core (the ha_mcp_tools custom
    #    component's "server" entry). Checked BEFORE the docker probe: the
    #    HA core container carries /.dockerenv, so without this branch
    #    embedded installs misreport as plain docker.
    if is_embedded():
        return "embedded"

    # 3. Home Assistant Add-on (has supervisor token)
    if is_running_in_addon():
        return "addon"

    # 4. Docker container (non-addon)
    if Path("/.dockerenv").exists():
        return "docker"

    # 5. Git clone - check for .git directory relative to package
    try:
        # Go up from tools_bug_report.py -> tools -> ha_mcp -> src -> project_root
        project_root = Path(__file__).parent.parent.parent.parent
        if (project_root / ".git").exists():
            return "git"
    except Exception:  # noqa: BLE001
        # Best-effort probe: path resolution may fail in unusual layouts;
        # fall through to the next detection heuristic.
        pass

    # 6. PyPI install - marker file exists in package
    try:
        marker_path = Path(__file__).parent.parent / "_pypi_marker"
        if marker_path.exists():
            return "pypi"
    except Exception:  # noqa: BLE001
        # Best-effort probe: marker lookup may fail in unusual layouts;
        # fall through to the default "unknown" result.
        pass

    # 7. Default - unknown
    return "unknown"


def _detect_installed_version() -> str | None:
    """Return the ha-mcp version installed ON DISK right now.

    ``__version__`` is frozen when the process first imports ha_mcp, so after
    an in-place update the running process keeps reporting the old version
    while the disk already carries the new one. Reporting both (plus
    ``version_mismatch``) turns a "which server am I talking to" hunt into a
    single tool call.
    """
    try:
        importlib.invalidate_caches()
        return get_version()
    except Exception as e:  # noqa: BLE001
        logger.info("Installed-version probe failed: %s", e)
        return None


def _instance_identity() -> dict[str, Any]:
    """Return process identity (id, start time, uptime).

    Mirrors the ``/api/settings/info`` fields so a report and the web UI can
    be matched to the same (or different) server process.
    """
    from ..settings_ui import _PROCESS_INSTANCE_ID, _PROCESS_STARTED_AT

    return {
        "instance_id": _PROCESS_INSTANCE_ID,
        "started_at": _PROCESS_STARTED_AT,
        "uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1),
    }


def _detect_platform() -> dict[str, str]:
    """Detect platform information."""
    return {
        "os": platform.system(),  # Windows, Darwin, Linux
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }


def _format_websockets_dependency_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the websockets probe for the human-pasteable report.

    Mirrors the sibling ``_format_*_value`` helpers: a degraded probe stays
    visibly degraded instead of disappearing — for the #2135/#2146 failure
    class this line IS the diagnosis, so it must survive into the report a
    user pastes.
    """
    state = diagnostic_info.get("websockets_dependency") or {}
    if not state:
        return "not probed"
    if state.get("vendored_import_ok"):
        value = f"vendored {state.get('vendored_version') or 'unknown'}"
        if state.get("vendored_c_speedups") is False:
            value += " (pure-Python)"
    else:
        value = (
            f"vendored BROKEN: {state.get('vendored_import_error', 'import failed')}"
        )
    if (error := state.get("shared_metadata_error")) is not None:
        shared = f"metadata unreadable: {error}"
    elif (version := state.get("shared_metadata_version")) is not None:
        shared = f"{version} per dist metadata"
    else:
        shared = "absent"
    return f"{value} | shared env copy: {shared}"


def _websockets_dependency_state() -> dict[str, Any]:
    """Report the vendored websockets copy plus the shared copy's state.

    ha-mcp runs on its private ``ha_mcp._vendor.websockets``, immune to the
    shared site-packages copy that ~20 HA integration libraries contend
    over (#2135/#2146). The vendored version proves what the server
    actually runs; the shared copy's metadata version is ecosystem context
    for triage (a torn shared copy no longer affects ha-mcp, but still
    breaks the integrations that use it). Any probe failure is captured as
    data — the bug-report path itself must never break on a broken
    dependency.
    """
    state: dict[str, Any] = {}
    try:
        vendored = importlib.import_module("ha_mcp._vendor.websockets")
        importlib.import_module("ha_mcp._vendor.websockets.asyncio.client")
        state["vendored_version"] = getattr(vendored, "__version__", "Unknown")
        state["vendored_import_ok"] = True
    except Exception as e:  # noqa: BLE001
        state["vendored_version"] = None
        state["vendored_import_ok"] = False
        state["vendored_import_error"] = f"{type(e).__name__}: {e}"
    # Whether the vendored copy has its optional C accelerator. The vendor
    # sync ships pure Python only (scripts/vendor_websockets.py strips the
    # compiled extension), so this answers "did vendoring cost throughput?"
    # without the reporter having to guess.
    state["vendored_c_speedups"] = (
        importlib.util.find_spec("ha_mcp._vendor.websockets.speedups") is not None
    )
    # Shared-copy context for triage, read from DIST METADATA only: ha-mcp
    # never imports the shared copy, and probing it by import would mean
    # touching the contested package this whole design avoids. Absent and
    # unreadable are reported distinctly — collapsing a corrupt install into
    # "absent" would read as a clean environment during exactly the failure
    # class this field exists for.
    try:
        state["shared_metadata_version"] = importlib.metadata.version("websockets")
    except importlib.metadata.PackageNotFoundError:
        state["shared_metadata_version"] = None
    except Exception as e:  # noqa: BLE001
        state["shared_metadata_version"] = None
        state["shared_metadata_error"] = f"{type(e).__name__}: {e}"
    return state


def _get_config_toggles(settings: Settings | None = None) -> dict[str, Any]:
    """Read tool-surface-shaping config toggles from Settings.

    Defaults to global settings; tests can pass Settings instead. Settings
    lookup failures return an empty dict; tool-state failures preserve the
    known settings and mark tool configuration diagnostics unavailable.
    """
    try:
        s = settings if settings is not None else get_global_settings()

        return collect_config_toggles(s)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to read settings for bug report toggles: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


def _tool_policy_summary() -> str:
    """Describe the stored tool security policy in one line, best-effort.

    The import stays inside the ``try``: the server keeps running when the
    policy package fails to import, and the report must still work then.
    """
    try:
        from ..policy.persistence import load_policy
        from ..utils.data_paths import get_data_dir

        policy = load_policy(get_data_dir())
    except Exception as e:  # noqa: BLE001
        logger.warning("Tool policy probe failed: %s (%s)", e, type(e).__name__)
        # load_policy's message can quote rule contents, so only its kind
        # reaches the report.
        if "not valid JSON" in str(e):
            return "unreadable (invalid JSON)"
        if "schema validation" in str(e):
            return "unreadable (schema validation failed)"
        return f"unreadable ({type(e).__name__})"
    count = len(policy.rules)
    rules = "1 rule" if count == 1 else f"{count} rules"
    return f"{rules}, rule_effect={policy.rule_effect}"


def _extract_client_info(ctx: Context | None) -> dict[str, str]:
    """Pull the connecting MCP client's self-identification off the request context.

    The MCP ``initialize`` handshake carries a ``clientInfo`` Implementation
    object (``name``/``version``/optional ``title``). FastMCP exposes the
    underlying server session as ``ctx.session``; the MCP SDK's
    ``ServerSession`` keeps the parsed initialize params on ``client_params``;
    on the 2026-07-28 protocol the SDK synthesizes them from the request's
    ``_meta`` when it carries both client info and capabilities. An
    older-protocol client over stateless HTTP sends client info only in
    ``initialize``, so the request's ``User-Agent`` is the fallback there,
    marked as such in ``title``.

    Returns ``{"name": ..., "version": ..., "title": ...}``. ``name`` and
    ``version`` fall back to ``"unknown"`` when the client didn't send them;
    ``title`` falls back to the empty string so callers can distinguish "not
    sent" from a real title without false-positive aside rendering.

    Returns an empty dict when there is no context, or no client info and no
    ``User-Agent`` (e.g. stdio), so the bug-report path stays robust. The
    log level is intentionally INFO, not DEBUG: this catch is the only signal
    we'd get if FastMCP/MCP SDK shape drifts in a future release, and silent
    drift would hide a regression for months.
    """
    if ctx is None:
        return {}
    try:
        session = getattr(ctx, "session", None)
        params = (
            getattr(session, "client_params", None) if session is not None else None
        )
        client = (
            getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
            if params is not None
            else None
        )
        if client is None:
            return _client_info_from_user_agent()
        return {
            "name": getattr(client, "name", None) or "unknown",
            "version": getattr(client, "version", None) or "unknown",
            "title": getattr(client, "title", None) or "",
        }
    except Exception as e:  # noqa: BLE001
        logger.info(
            "Failed to read MCP client info from context: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


def _client_info_from_user_agent() -> dict[str, str]:
    """``{"name", "version", "title"}`` from the HTTP ``User-Agent``, or ``{}``."""
    user_agent = get_http_headers(include={"user-agent"}).get("user-agent", "").strip()
    if not user_agent:
        return {}
    product = user_agent.split()[0]
    name, _, version = product.partition("/")
    logger.debug("No MCP client info on the request; using User-Agent %r", product)
    return {
        "name": name or "unknown",
        "version": version or "unknown",
        "title": "from HTTP User-Agent",
    }


def _http_user_agent() -> str:
    """The request's ``User-Agent``, or ``""`` outside an HTTP request."""
    value = get_http_headers(include={"user-agent"}).get("user-agent", "")
    return str(value).strip()


def _detect_mcp_transport() -> str:
    """Best-effort MCP transport detection.

    Returns ``stdio`` / ``http`` / ``sse`` / ``unknown``. We can't observe the
    transport perfectly from a tool call, so we look at the entrypoint name
    and well-known env hints. The result is informational — the bug template
    surfaces it as an auto-detect that the agent or user can override.

    ha-mcp ships no SSE entry point, so ``sse`` only appears when an operator
    drives fastmcp's own deprecated SSE transport via ``FASTMCP_TRANSPORT`` —
    worth surfacing in a report, since that run shape is unsupported.
    """
    # Entry-point script name (e.g. ``ha-mcp-web`` for HTTP;
    # pyproject.toml's [project.scripts] is the source of truth).
    argv0 = (sys.argv[0] if sys.argv else "").lower()
    basename = os.path.basename(argv0)
    if basename.endswith("-web"):
        return "http"

    # Env hints set by HTTP wrappers / supervisors. ``streamable-http`` is the
    # documented FastMCP variant; collapse it to ``http`` since the
    # distinction doesn't change triage decisions.
    transport_env = os.environ.get("FASTMCP_TRANSPORT", "").strip().lower()
    if transport_env in {"http", "stdio", "sse"}:
        return transport_env
    if transport_env == "streamable-http":
        return "http"
    if os.environ.get("MCP_HTTP_PORT") or os.environ.get("FASTMCP_PORT"):
        return "http"

    # The in-process server inside HA core only ever serves HTTP, and HA's
    # stdin is not a TTY, so the isatty fallback below would label every
    # embedded install "stdio" (seen on a live component install, 8.5.0).
    if is_embedded():
        return "http"

    # Home Assistant add-on always runs HTTP via homeassistant-addon/start.py.
    # Placed after the explicit hints (argv0 / env) so an operator override
    # still wins, but before the stdin-isatty fallback because Supervisor-
    # launched containers have no TTY on stdin and would otherwise be
    # mislabeled stdio.
    if is_running_in_addon():
        return "http"

    # If stdin is piped (not a TTY), ha-mcp was launched by an MCP host on
    # stdio. If it IS a TTY, this is a manual / interactive run with no
    # other transport hints — fall through to ``unknown``.
    try:
        if not sys.stdin.isatty():
            return "stdio"
    except (AttributeError, OSError, ValueError):
        # ``sys.stdin`` can be None or detached (pythonw, daemonized
        # contexts, certain test harnesses). Treat as no signal.
        pass

    return "unknown"


async def _fetch_addon_logs() -> str:
    """Fetch ha-mcp addon container logs via the Supervisor REST API.

    Only works when running as a Home Assistant add-on (SUPERVISOR_TOKEN set).
    Uses /addons/self/logs which resolves to the calling addon's own logs via
    the Supervisor's per-addon token binding — no slug interpolation needed.

    Direct httpx against ``http://supervisor`` is the documented add-on access
    pattern: it uses the Supervisor token directly (no extra HA hop) and
    preserves the ``self`` shortcut, which the WebSocket ``supervisor/api``
    proxy used by other tools may not.

    Returns sanitized log text (last _ADDON_LOG_MAX_CHARS chars, with a
    truncation marker prepended when truncation occurs), or empty string on
    failure.
    """
    # Redundant with the caller's `install_method == "addon"` gate, but kept
    # as a defensive guard for any direct callers added later.
    if not is_running_in_addon():
        return ""

    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=get_global_settings().verify_ssl
        ) as http_client:
            resp = await http_client.get("/addons/self/logs")
            if resp.status_code != 200:
                logger.info("Addon log fetch returned HTTP %s", resp.status_code)
                return ""

            # Strip ANSI escape codes first, then sanitize, then truncate.
            # Sanitizing before truncating prevents secrets that straddle the
            # truncation boundary from leaking through.
            cleaned = ANSI_ESCAPE_RE.sub("", resp.text)
            sanitized = _sanitize_log_text(cleaned)
            if len(sanitized) > _ADDON_LOG_MAX_CHARS:
                marker = (
                    f"[...truncated, showing last {_ADDON_LOG_MAX_CHARS} of "
                    f"{len(sanitized)} chars...]\n"
                )
                return marker + sanitized[-_ADDON_LOG_MAX_CHARS:]
            return sanitized
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch addon logs: {e}")

    return ""


async def _fetch_core_error_log(client: Any) -> str:
    """Fetch the Home Assistant error log (home-assistant.log) via the client.

    Works on every install type — ``HomeAssistantClient.get_error_log`` routes
    add-on installs to the Supervisor, supervised/HAOS to the hassio proxy, and
    container/pip to ``/api/error_log``. Captured over REST, which stays up even
    when the WebSocket path is failing; that is exactly the failure mode in
    issue #1694, where the high-value auth lines
    (``InsecureKeyLengthWarning``, "invalid authentication") only appeared in
    home-assistant.log, not in the add-on log this tool already captured.

    Best-effort: returns sanitized text (last ``_CORE_LOG_MAX_CHARS`` chars of a
    ``_CORE_LOG_WINDOW_LINES`` window, with a truncation marker prepended) or an
    empty string on any failure, so a log-fetch problem never breaks the
    bug-report path.
    """
    try:
        page = await client.get_error_log(lines=_CORE_LOG_WINDOW_LINES)
    except Exception as e:  # noqa: BLE001
        # Broad by design — the bug-report path must stay robust whatever the
        # client raises (auth, role, connection, transport). Logged at INFO so
        # a missing error log is visible without alarming on a routine 403.
        logger.info(
            "Could not fetch HA error log for bug report: %s (%s)",
            e,
            type(e).__name__,
        )
        return ""

    if not page.text:
        return ""

    # Strip ANSI, then sanitize, then truncate — sanitizing before truncating
    # keeps a secret straddling the cut boundary from leaking through.
    sanitized = _sanitize_log_text(ANSI_ESCAPE_RE.sub("", page.text))
    if len(sanitized) > _CORE_LOG_MAX_CHARS:
        marker = (
            f"[...truncated, showing last {_CORE_LOG_MAX_CHARS} of "
            f"{len(sanitized)} chars...]\n"
        )
        return marker + sanitized[-_CORE_LOG_MAX_CHARS:]
    return sanitized


# Entry titles assigned by the component's config flow (config_flow.py /
# const.py in custom_components/ha_mcp_tools) — the server code cannot import
# the separately-shipped component package, so the defaults are mirrored here.
# "HA MCP Tools" is the pre-#1853 tools-entry title an unloaded legacy entry
# may still carry. A user-renamed entry lands in the "unrecognized" bucket
# rather than being misclassified.
_SERVER_ENTRY_TITLE = "HA-MCP Server"
_TOOLS_ENTRY_TITLES = frozenset({"HA-MCP File & YAML Tools", "HA MCP Tools"})


def _classify_component_entries(
    entries: list[Any],
) -> tuple[list[str], list[str]]:
    """Split ha_mcp_tools config entries into server-entry and unrecognized.

    Tools-entry titles are dropped: their functional state comes from the
    services probe (``_detect_tools_entry_status``), which sees whether the
    entry actually serves, not just whether it exists.
    """
    server: list[str] = []
    unrecognized: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "untitled")
        state = str(entry.get("state") or "unknown state")
        if title == _SERVER_ENTRY_TITLE:
            server.append(f"added ({state})")
        elif title not in _TOOLS_ENTRY_TITLES:
            unrecognized.append(f'"{title}" ({state})')
    return server, unrecognized


def _build_formatted_report(
    diagnostic_info: dict[str, Any],
    mcp_transport: str,
    client_info: dict[str, str],
    platform_info: dict[str, str],
    config_toggles: dict[str, Any],
    startup_logs: list[dict[str, Any]],
    startup_log_summary: str,
    recent_logs: list[dict[str, Any]],
    log_summary: str,
    addon_logs: str,
    core_error_log: str,
) -> str:
    report_lines = [
        "=== ha-mcp Bug Report Info ===",
        "",
        f"ha-mcp Version: {_format_version_value(diagnostic_info)}",
        f"Custom Component: {diagnostic_info.get('component_version') or 'not detected (not installed, or probe failed)'}",
        f"File & YAML Tools entry: {_format_tools_entry_value(diagnostic_info)}",
        f"In-process Server entry: {_format_server_entry_value(diagnostic_info)}",
        f"Installation Method: {diagnostic_info['installation_method']}",
        f"MCP Transport: {mcp_transport}",
        f"MCP Client: {_format_client_info_for_template(client_info)}",
        f"MCP Client Host: {_format_client_host_for_template(diagnostic_info)}",
        f"Operating System: {platform_info['os']} {platform_info['os_release']} ({platform_info['architecture']})",
        f"Python Version: {platform_info['python_version']}",
        f"Home Assistant Version: {diagnostic_info['home_assistant_version']}",
        f"Supervisor: {_format_supervisor_value(diagnostic_info)}",
        f"Connection Status: {diagnostic_info['connection_status']}",
        f"Entity Count: {diagnostic_info['entity_count']}",
        f"websockets Dependency: {_format_websockets_dependency_value(diagnostic_info)}",
    ]
    if "location_name" in diagnostic_info:
        report_lines.append(f"Location Name: {diagnostic_info['location_name']}")
    if "time_zone" in diagnostic_info:
        report_lines.append(f"Time Zone: {diagnostic_info['time_zone']}")
    if config_toggles:
        report_lines.extend(["", "=== ha-mcp Config Toggles ==="])
        for key, value in config_toggles.items():
            report_lines.append(f"  {key}: {value}")
    report_lines.append(
        f"Tool Policy: {diagnostic_info.get('tool_policy', 'not probed')}"
    )
    if startup_logs:
        report_lines.extend(
            [
                "",
                f"=== Startup Logs ({len(startup_logs)} entries) ===",
                startup_log_summary,
            ]
        )
    if recent_logs:
        report_lines.extend(
            [
                "",
                f"=== Recent Tool Calls ({len(recent_logs)} entries) ===",
                log_summary,
            ]
        )
    if addon_logs:
        report_lines.extend(["", "=== Add-on Container Logs ===", addon_logs])
    if core_error_log:
        report_lines.extend(["", "=== Home Assistant Error Log ===", core_error_log])
    return "\n".join(report_lines)


class BugReportTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _detect_component_version(self) -> str | None:
        """Read the ha_mcp_tools custom component's version, best-effort.

        Prefers the shared cached capability probe (``get_component_caps`` — one
        ``ha_mcp_tools/info`` round-trip per client): a component new enough to
        answer it (1.1.0+) reports its version there, so the report reuses that
        cached probe instead of a bespoke round-trip. A component in the
        0.11.0-1.1.0 band has services but no ``info`` command (caps is None),
        so it falls back to the ``get_caller_token`` bootstrap service, whose
        response also carries the manifest version. Returns None when the
        component is not installed or the call fails — the report path must
        never break on it.
        """
        # get_component_caps never raises (it caches/returns None on every
        # failure mode), so no guard is needed around it.
        caps = await get_component_caps(self._client)
        if caps is not None:
            return caps.component_version or None
        try:
            resp = await self._client.call_service(
                "ha_mcp_tools", "get_caller_token", {}, return_response=True
            )
            payload = (
                resp.get("service_response", resp) if isinstance(resp, dict) else {}
            )
            version = payload.get("version") if isinstance(payload, dict) else None
            return str(version) if version else None
        except Exception as e:  # noqa: BLE001
            logger.info("Component version probe failed: %s", e)
            return None

    async def _detect_tools_entry_status(self) -> str | None:
        """Describe the File & YAML Tools entry state for the report, best-effort.

        The ``ha_mcp_tools`` services register only in that entry's setup, so
        the service registry distinguishes "entry set up" from the #1996
        state — integration installed but the second entry never added —
        which the component version alone cannot show. Returns None when the
        probe fails; the report path must never break on it.
        """
        from .tools_filesystem import _bootstrap_service_state

        try:
            domain_registered, bootstrap_registered = await _bootstrap_service_state(
                self._client
            )
        except Exception as e:  # noqa: BLE001
            logger.info("Tools-entry status probe failed: %s", e)
            return None
        if not domain_registered:
            return (
                "not set up — no ha_mcp_tools services registered; add the "
                '"HA-MCP File & YAML Tools" entry via "Add entry" on the '
                "HA-MCP Custom Component integration (or install the "
                "component first)"
            )
        if not bootstrap_registered:
            return "set up, but the component is pre-0.5.0 (update via HACS)"
        return "set up (ha_mcp_tools services registered)"

    async def _detect_server_entry_status(self) -> str | None:
        """Describe the in-process "HA-MCP Server" entry state, best-effort.

        Both parts of the custom component ship under the single
        ``ha_mcp_tools`` domain, so "the component is installed" says nothing
        about WHICH part is set up. The File & YAML tools part is covered by
        the services probe; this one classifies the domain's config entries by
        their flow-assigned titles to show whether the in-process server entry
        exists — e.g. an add-on install that erroneously also added it (a
        second, redundant server) becomes visible next to Installation Method.
        Returns None when the probe fails; the report must never break on it.
        """
        try:
            response = await self._client.send_websocket_message(
                {"type": "config_entries/get", "domain": "ha_mcp_tools"}
            )
            if not isinstance(response, dict) or not response.get("success"):
                return None
            result = response.get("result")
            if not isinstance(result, list):
                return None
        except Exception as e:  # noqa: BLE001
            logger.info("Server-entry status probe failed: %s", e)
            return None
        server, unrecognized = _classify_component_entries(result)
        if server:
            return ", ".join(server)
        if unrecognized:
            # A renamed entry cannot be classified by title — report it
            # rather than claiming the server entry is absent.
            return "not identified — unrecognized ha_mcp_tools entries: " + ", ".join(
                unrecognized
            )
        return "not added"

    async def _detect_supervisor_info(self) -> dict[str, str]:
        """Read the Supervisor version and host OS, best-effort.

        App tools go through the Supervisor, so a report about them needs its
        release (#2270, #2278). Called only on a supervised install; a failure
        keeps its error type so a timeout or a rejected token is visible.
        """
        from .tools_addons import _supervisor_api_call

        try:
            response = await asyncio.wait_for(
                _supervisor_api_call(self._client, "/info"),
                timeout=_SUPERVISOR_PROBE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            logger.info("Supervisor info probe failed: %s (%s)", e, type(e).__name__)
            return {"error": type(e).__name__}
        info = response.get("result")
        if not isinstance(info, dict):
            return {"error": "unexpected response"}
        return {
            "supervisor_version": str(info.get("supervisor") or "unknown"),
            "host_os": str(info.get("operating_system") or "unknown"),
        }

    async def _add_home_assistant_info(self, diagnostic_info: dict[str, Any]) -> None:
        """Fill in connection status, HA config and entity count; never raises."""
        try:
            config = await self._client.get_config()
            diagnostic_info["connection_status"] = "Connected"
            diagnostic_info["home_assistant_version"] = config.get("version", "Unknown")
            diagnostic_info["location_name"] = config.get("location_name", "Unknown")
            diagnostic_info["time_zone"] = config.get("time_zone", "Unknown")
            # Only a supervised install loads the hassio integration. Probing
            # without it only produces an "Unknown command" error.
            if "hassio" in config.get("components", []):
                diagnostic_info["supervisor"] = await self._detect_supervisor_info()
            else:
                diagnostic_info["supervisor"] = {"none": "true"}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to get Home Assistant config: {e}")
            diagnostic_info["connection_status"] = (
                f"Connection Error: {_sanitize_log_text(str(e))}"
            )

        try:
            states = await self._client.get_states()
            if states:
                diagnostic_info["entity_count"] = len(states)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to get entity count: {e}")

    @tool(
        name="ha_report_issue",
        tags={"Utilities"},
        annotations=read_only_hints("Report Issue or Feedback", open_world=False),
    )
    @log_tool_usage
    async def ha_report_issue(
        self,
        report_type: Annotated[
            Literal["runtime_bug", "agent_behavior", "feature_request"],
            Field(
                default="runtime_bug",
                description=(
                    "'runtime_bug' when ha-mcp errored or behaved wrongly; "
                    "'agent_behavior' when the user says you used the wrong "
                    "tool or worked inefficiently; 'feature_request' when the "
                    "user wants ha-mcp to do something it cannot do yet."
                ),
            ),
        ] = "runtime_bug",
        title: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "One-line summary of what broke or what is requested, for "
                    "the issue title."
                ),
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "What went wrong in markdown: steps to reproduce, expected "
                    "and actual behavior. For agent_behavior: what you did and "
                    "what you should have done. For feature_request: what ha-mcp "
                    "should do, why the user needs it, and what you tried."
                ),
            ),
        ] = None,
        user_prompt: Annotated[
            str | None,
            Field(
                default=None,
                description="The user message that led to the problem, verbatim.",
            ),
        ] = None,
        user_comment: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "The user's own words on what went wrong or what bothered "
                    "them, verbatim. Ask the user for it; never write it "
                    "yourself."
                ),
            ),
        ] = None,
        tool_calls: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "The tool call(s) that produced the problem, verbatim: name, "
                    "arguments and the (shortened) response."
                ),
            ),
        ] = None,
        ai_model: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Your own model identity, as specific as you know it. Do not "
                    "invent a version."
                ),
            ),
        ] = None,
        client_app: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "The app and version the user runs you in, as the USER "
                    "states it (usually on the app's About screen or from its "
                    "--version command). Never guess it."
                ),
            ),
        ] = None,
        tool_call_count: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                le=16,
                description=(
                    "Number of ha_* tool calls made since the issue started; determines how"
                    " many log entries to include."
                ),
            ),
        ] = 10,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level response keys. "
                    "None = full response. Typical: "
                    "'issue_title,issue_body,issue_url,duplicate_check_urls,"
                    "anonymization_guide,missing_tool_hint,"
                    "known_client_issues_hint,instructions'. issue_body "
                    "already embeds the relevant logs, so the raw log keys are "
                    "only needed for your own analysis. "
                    "Available keys: diagnostic_info, recent_logs, "
                    "startup_logs, addon_logs, core_error_log, log_count, "
                    "startup_log_count, formatted_report, issue_title, "
                    "issue_body, issue_url, anonymization_guide, "
                    "duplicate_check_urls, missing_tool_hint, "
                    "known_client_issues_hint, instructions."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get diagnostics and a finished GitHub issue for a bug, agent feedback or a feature request.

        Use it before filing any GitHub issue about ha-mcp: a bug report
        without this report is closed after 24 hours. `report_type` picks
        the kind of report; if it is unclear which, ask: "Are you reporting a
        bug in ha-mcp, giving feedback on how I used the tools, or requesting
        a feature?"

        Pass the report text in the call: the server combines it with the
        diagnostics it collects into `issue_title`, `issue_body` (the full
        report with logs) and `issue_url` (a new-issue link with title and
        body filled in, log sections left out to fit GitHub's URL limit;
        error messages stay in, with secrets redacted). A call
        without text still returns diagnostics, with placeholders in the body.

        The response is LARGE; `fields=` narrows it. Read `instructions`
        before showing anything to the user: it covers the missing-tool and
        known-client pre-checks, the duplicate check, the mandatory
        anonymisation step, and how to file the issue. Check
        `missing_tool_hint` FIRST when the report is about a missing tool: a
        stale client tool list (not a bug) is the usual cause.
        """
        # Validate fields= before anything is collected: the projection at the
        # end was the only parse, outside any ValueError handler, so a
        # malformed value reached FastMCP as a bare exception after the whole
        # report had been assembled.
        parsed_fields: list[str] | None = None
        if fields is not None:
            try:
                parsed_fields = parse_string_list_param(
                    fields, "fields", allow_csv=True
                )
            except ValueError as exc:
                raise_tool_error(create_validation_error(str(exc), parameter="fields"))

        text = _ReportText(
            title=title,
            description=description,
            user_prompt=user_prompt,
            tool_calls=tool_calls,
            user_comment=user_comment,
            ai_model=ai_model,
            client_app=client_app,
        )

        # Detect installation method, platform, and runtime config.
        install_method = _detect_installation_method()
        platform_info = _detect_platform()
        config_toggles = _get_config_toggles()
        tool_policy = await asyncio.to_thread(_tool_policy_summary)
        mcp_transport = _detect_mcp_transport()
        client_info = _extract_client_info(ctx)
        client_host = (
            await asyncio.to_thread(detect_client_host)
            if mcp_transport == "stdio"
            else {}
        )
        user_agent = _http_user_agent()
        installed_version = await asyncio.to_thread(_detect_installed_version)
        component_version = await self._detect_component_version()
        tools_entry_status = await self._detect_tools_entry_status()
        server_entry_status = await self._detect_server_entry_status()

        diagnostic_info: dict[str, Any] = {
            "ha_mcp_version": __version__,
            "installed_version": installed_version,
            "version_mismatch": bool(
                installed_version and installed_version != __version__
            ),
            "component_version": component_version,
            "tools_entry_status": tools_entry_status,
            "server_entry_status": server_entry_status,
            "supervisor": {"error": "not probed"},
            "instance": _instance_identity(),
            "installation_method": install_method,
            "platform": platform_info,
            "websockets_dependency": _websockets_dependency_state(),
            "mcp_transport": mcp_transport,
            "mcp_client_info": client_info,
            "mcp_client_host": client_host,
            "http_user_agent": user_agent,
            "config_toggles": config_toggles,
            "ignored_disabled_tools": config_toggles.get("ignored_disabled_tools"),
            "tool_config_warnings": config_toggles.get("tool_config_warnings"),
            "tool_config_status": config_toggles.get("tool_config_status"),
            "tool_policy": tool_policy,
            "connection_status": "Unknown",
            "home_assistant_version": "Unknown",
            "entity_count": 0,
        }

        await self._add_home_assistant_info(diagnostic_info)

        # Calculate how many log entries to retrieve
        # Formula: AVG_LOG_ENTRIES_PER_TOOL * 4 * tool_call_count (doubled from 2x to 4x)
        max_log_entries = AVG_LOG_ENTRIES_PER_TOOL * 4 * tool_call_count
        recent_logs = get_recent_logs(max_entries=max_log_entries)

        # Get startup logs (first minute of server operation)
        startup_logs = get_startup_logs()

        # Fetch addon container logs when running as HA add-on
        addon_logs = ""
        if install_method == "addon":
            addon_logs = await _fetch_addon_logs()

        # Fetch the Home Assistant error log (home-assistant.log) on every
        # install type. It's pulled over REST — which stays up even when the
        # WebSocket path is failing — so the report captures auth / integration
        # errors (issue #1694) that never appear in the add-on log above.
        core_error_log = await _fetch_core_error_log(self._client)

        # Format logs for inclusion (sanitized summary)
        log_summary = _format_logs_for_report(recent_logs)
        startup_log_summary = _format_startup_logs(startup_logs)

        # Build the formatted report
        formatted_report = _build_formatted_report(
            diagnostic_info,
            mcp_transport,
            client_info,
            platform_info,
            config_toggles,
            startup_logs,
            startup_log_summary,
            recent_logs,
            log_summary,
            addon_logs,
            core_error_log,
        )

        # Without the agent's title, a fallback keeps reports from being filed
        # under a bare prefix: the last error for a bug, a fixed name for a
        # feature request.
        suggested_title = _suggested_title(report_type, diagnostic_info, recent_logs)
        issue_title = _issue_title(report_type, text, suggested_title)

        def build_body(include_logs: bool, text_cap: int | None = None) -> str:
            if report_type == "runtime_bug":
                return _generate_runtime_bug_template(
                    diagnostic_info,
                    log_summary,
                    startup_log_summary,
                    recent_logs,
                    startup_logs,
                    addon_logs=addon_logs,
                    core_error_log=core_error_log,
                    text=text,
                    include_logs=include_logs,
                    text_cap=text_cap,
                )
            return _AGENT_SIDE_TEMPLATES[report_type](
                diagnostic_info,
                log_summary,
                text=text,
                include_logs=include_logs,
                text_cap=text_cap,
            )

        issue_body = build_body(include_logs=True)
        issue_url = _build_issue_url(
            issue_title, lambda cap: build_body(include_logs=False, text_cap=cap)
        )

        # Generate search keywords and URLs for duplicate check
        search_keywords = _generate_search_keywords(
            diagnostic_info,
            recent_logs,
            text.title if report_type == "feature_request" else None,
        )
        duplicate_check_urls = [
            f"https://github.com/homeassistant-ai/ha-mcp/issues?q=is%3Aissue+{quote_plus(keyword)}"
            for keyword in search_keywords[:3]  # Limit to top 3 keywords
        ]

        result: dict[str, Any] = {
            "success": True,
            "diagnostic_info": diagnostic_info,
            "recent_logs": recent_logs,
            # Scrub the startup-log *messages* — they aren't sanitized at source,
            # so the connect-URL secret path can otherwise surface here.
            # recent_logs is also returned, but its sensitive *parameters* are
            # key-masked at the logging chokepoint and its error_message is
            # returned as-is to the trusted client by design (see SECURITY.md);
            # formatted_report and addon_logs already route through
            # _sanitize_log_text. This copies entries — the underlying log
            # records stay raw.
            "startup_logs": [
                {**entry, "message": _sanitize_log_text(entry.get("message", ""))}
                for entry in startup_logs
            ],
            "addon_logs": addon_logs,
            # Already sanitized + truncated by _fetch_core_error_log.
            "core_error_log": core_error_log,
            "log_count": len(recent_logs),
            "startup_log_count": len(startup_logs),
            "formatted_report": formatted_report,
            "issue_title": issue_title,
            "issue_body": issue_body,
            "issue_url": issue_url,
            "anonymization_guide": _generate_anonymization_guide(),
            "duplicate_check_urls": duplicate_check_urls,
            "missing_tool_hint": MISSING_TOOL_HINT,
            "known_client_issues_hint": KNOWN_CLIENT_ISSUES_HINT,
            "instructions": (
                "WORKFLOW FOR FILING A REPORT:\n\n"
                "0. **PRE-CHECK — is the problem a missing/unavailable tool?** If "
                "the user's issue is that a tool they expected is missing or "
                "cannot be called, DO NOT file a bug yet. See the "
                "`missing_tool_hint` field: the likely cause is a stale MCP "
                "client tool list (not a server bug), fixed by refreshing or "
                "reconnecting the MCP connection. Only continue with this report "
                "if the tool is still missing after the user refreshes.\n\n"
                "0b. **PRE-CHECK — is it a known Claude Desktop bug?** A write "
                "tool that hangs to a 4-minute timeout, or every call failing "
                "with 'expected nonoptional, received undefined', is a client "
                "bug with an upstream ticket. See the `known_client_issues_hint` "
                "field for the workaround to give the user, and only continue "
                "if the problem persists after it.\n\n"
                "1. **Check for duplicates FIRST**:\n"
                "   - Use the duplicate_check_urls to search for similar issues\n"
                '   - If gh CLI is available: use `gh issue list --search "keyword"`\n'
                "   - Otherwise: inform user to check the duplicate_check_urls\n"
                "   - If duplicates found, ask user if they want to comment on existing issue instead\n\n"
                "2. **Pass the report text** (call ha_report_issue again if this "
                "call had none): report_type, title, description, user_comment, "
                "user_prompt, tool_calls, ai_model and client_app. The server builds "
                "issue_title, issue_body and issue_url from them. Anonymize the "
                "text first (step 3).\n"
                "   - report_type: 'runtime_bug' when ha-mcp errored or behaved "
                "wrongly; 'agent_behavior' when the user says YOU used the wrong "
                "tool, should have done something differently, or worked "
                "inefficiently; 'feature_request' when the user wants ha-mcp "
                "to do something it cannot do yet. For a feature request, "
                "first try to do it with the tools you have: the tool calls "
                "show the feature is missing. If unclear, ASK: 'Are you "
                "reporting a bug in ha-mcp, giving feedback on how I used the "
                "tools, or requesting a feature?'\n"
                "   - user_prompt and tool_calls: the EXACT user message and the "
                "tool call(s) that produced the problem, copied verbatim. This "
                "is the single most useful part for triage. Do not skip it.\n"
                "   - user_comment: ASK the user to say in their own words what "
                "went wrong or what bothered them, and pass it verbatim. Never "
                "write it yourself: maintainers need the user's view, not "
                "yours.\n"
                "   - ai_model: your own identity, as specific as you know it. "
                "Do not invent a version number.\n"
                "   - client_app: the **MCP Client Host:** line in issue_body is "
                'auto-detected. If it says "not detected", "stdio bridge", '
                'or the version is "unknown", ASK the user which app and '
                "version they are using (usually on the app's About screen or "
                "from its --version command) and pass THEIR answer. NEVER "
                "fill it in yourself: you cannot know the app or its version, "
                "and a guessed value sends triage the wrong way. If the user "
                'does not know, pass "unknown (user asked)". Client-side '
                "regressions often depend on the exact app release.\n\n"
                "3. **ANONYMIZE** (CRITICAL), both in the text you pass and in "
                "the logs inside issue_body:\n"
                "   a. Replace person names with generic labels (person.user1, person.user2)\n"
                "   b. Replace location names with generic names (Home, Location1)\n"
                "   c. Replace device names containing personal info (e.g., 'juliens_bedroom') with generic ones (e.g., 'bedroom_1')\n"
                "   d. Verify no tokens, passwords, or IPs are visible\n"
                "   e. Keep entity domains, error messages, and technical details\n"
                "   See anonymization_guide for full details. Apart from these "
                "replacements, pass issue_body on UNCHANGED: do not summarize, "
                "shorten or rewrite it.\n\n"
                "4. **Show the user issue_title and issue_body, then file it "
                "the first way you can.** The issue must contain issue_body "
                "itself, starting with its Auto-Generated heading: a bug "
                "report that carries a summary or your own write-up instead "
                "is closed after 24 hours.\n"
                "   a. You can act on GitHub (gh CLI, a GitHub connector, or a "
                "browser you control): file the issue only after the user says "
                "yes.\n"
                "      - gh: save issue_title and issue_body to files, then run "
                "`gh issue create --repo homeassistant-ai/ha-mcp --title "
                '"$(cat <title-file>)" --body-file <body-file>`. Never put the '
                "title text itself on the command line: the shell would run "
                "backticks or $(...) inside it.\n"
                "      - Browser: open https://github.com/homeassistant-ai/ha-mcp/issues/new "
                "in the user's signed-in browser, set the title and description "
                "fields directly (fill or set the value; do not type it key by "
                "key), and let the user review it and click Create.\n"
                "   b. You can write files: save issue_body as a .md file. Tell "
                "the user to open issue_url, replace the pre-filled description "
                "with the file's content, and click Create.\n"
                "   c. Chat only: give the user issue_url prominently. It opens "
                "a new issue with the title and report filled in, without the "
                "logs. Then show issue_body in a code block fenced with FOUR "
                "backticks (````markdown ... ````) so it stays in one piece, and "
                "ask the user to paste its log sections into the issue. You "
                "cannot edit the link's text, and its error messages come from "
                "the server: if they show personal names, tell the user to "
                "replace them in GitHub's editor. Remind them to check the "
                "pre-filled text for personal information before clicking "
                "Create.\n\n"
                f"5. The last line of issue_body is `{REPORT_END_MARKER}`. A "
                "copy without it was cut short.\n\n"
                "CRITICAL: Always ANONYMIZE before showing or filing the report!"
            ),
        }
        return project_fields(result, parsed_fields)


def register_bug_report_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register bug report tools with the MCP server."""
    register_tool_methods(mcp, BugReportTools(client))


def _format_logs_for_report(logs: list[dict[str, Any]]) -> str:
    """Format log entries for inclusion in a bug report."""
    if not logs:
        return "(No recent logs available)"

    lines = []
    for log in logs:
        timestamp = log.get("timestamp", "?")[:19]  # Trim to seconds
        tool_name = log.get("tool_name", "unknown")
        success = "OK" if log.get("success") else "FAIL"
        exec_time = log.get("execution_time_ms", 0)
        error = log.get("error_message", "")

        line = f"  {timestamp} | {tool_name} | {success} | {exec_time:.0f}ms"
        if error:
            # Sanitize before truncating so secrets straddling the cut survive redaction.
            error_short = _sanitize_log_text(str(error))[:100]
            line += f" | Error: {error_short}"
        lines.append(line)

    return "\n".join(lines)


def _format_startup_logs(logs: list[dict[str, Any]]) -> str:
    """Format startup log entries for inclusion in a bug report."""
    if not logs:
        return "(No startup logs available)"

    lines = []
    for log in logs:
        elapsed = log.get("elapsed_seconds", 0)
        level = log.get("level", "INFO")
        logger_name = log.get("logger", "")
        message = log.get("message", "")

        # Sanitize before truncating so secrets straddling the cut survive redaction.
        message = _sanitize_log_text(message)
        if len(message) > 200:
            message = message[:200] + "..."

        line = f"  +{elapsed:05.2f}s | {level:5} | {logger_name}: {message}"
        lines.append(line)

    return "\n".join(lines)


def _issue_title(report_type: str, text: _ReportText, suggested_title: str) -> str:
    """Return a one-line issue title carrying the report type's prefix."""
    prefix = _TITLE_PREFIXES[report_type].strip()
    title = " ".join((text.title or "").split())
    if title.upper().startswith(prefix):
        title = title[len(prefix) :].strip()
    # A title that was only the prefix or whitespace says nothing.
    title = f"{prefix} {title or ' '.join(suggested_title.split())}"
    if len(title) > _ISSUE_TITLE_MAX_CHARS:
        title = title[: _ISSUE_TITLE_MAX_CHARS - 3] + "..."
    return title


def _generate_anonymization_guide() -> str:
    """Generate privacy/anonymization instructions."""
    return """## Anonymization Guide

Before submitting your bug report, please review and anonymize:

### MUST ANONYMIZE (security-sensitive):
- API tokens, passwords, secrets -> Replace with "[REDACTED]"
- IP addresses (internal/external) -> Replace with "192.168.x.x" or "[IP]"
- MAC addresses -> Replace with "[MAC]"
- Email addresses -> Replace with "user@example.com"
- Phone numbers -> Replace with "[PHONE]"

### CONSIDER ANONYMIZING (privacy-sensitive):
- Location names (city, address) -> Replace with generic names like "Home" or "[LOCATION]"
- Device names that reveal personal info -> Replace with "Device 1", "Light 1", etc.
- Person names in entity IDs -> Replace with "person.user1"
- Calendar/todo items with personal details -> Summarize without specifics

### KEEP AS-IS (helpful for debugging):
- Entity domains (light, switch, sensor, etc.)
- Device types and capabilities
- Automation/script structure (triggers, conditions, actions)
- Error messages (but check for secrets in them)
- Timestamps and durations
- State values (on/off, numeric values, etc.)
- Home Assistant and ha-mcp versions

### Example anonymization:
BEFORE: "light.juliens_bedroom" with token "eyJhbG..."
AFTER:  "light.bedroom_1" with token "[REDACTED]"

The goal is to preserve enough detail to reproduce and fix the bug
while protecting your personal information and security.
"""
