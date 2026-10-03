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
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus, urlencode

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
from .coercion import ANSI_ESCAPE_RE, JSON_STRING_COERCION, parse_string_list_param
from .component_api import get_component_caps
from .helpers import (
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .response_helpers import project_fields
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

NEW_ISSUE_URL = "https://github.com/homeassistant-ai/ha-mcp/issues/new"

# GitHub rejects an /issues/new URL of about 8 KB or more with 414 URI Too
# Long (measured 2026-09-30: 8,167 characters loaded, 8,217 did not). That is
# observed behaviour, not a documented limit, so the budget stays below it.
_ISSUE_URL_MAX_CHARS = 7500

# Last line of every generated issue body. A paste that lost its end, for
# example because a chat UI closed the code block early, is visibly short.
REPORT_END_MARKER = "<!-- end of ha_report_issue report -->"

_TITLE_PREFIXES = {"runtime_bug": "[BUG] ", "agent_behavior": "[AGENT] "}

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

# IPv4 sanitization: only redact addresses with strong network context so that
# four-segment version strings (e.g. "ha-mcp version 1.2.3.4") are preserved.
_IPV4_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = rf"(?:{_IPV4_OCTET}\.){{3}}{_IPV4_OCTET}"
# IP followed by :port or /CIDR — always a network address, never a version.
_IPV4_WITH_PORT_OR_CIDR_RE = re.compile(rf"\b{_IPV4}(?::\d+|/\d{{1,2}})\b(?!\.\d)")
# IP preceded by a network keyword (from, to, host=, addr=, etc.).
_IPV4_AFTER_KEYWORD_RE = re.compile(
    rf"\b((?:from|to|host|hostname|addr|address|ip|src|dst|server|client|peer|via)\b\s*[=:]?\s*){_IPV4}\b(?!\.\d)",
    re.IGNORECASE,
)
# IP appearing inside a URL (`scheme://1.2.3.4...`).
_IPV4_IN_URL_RE = re.compile(rf"(://){_IPV4}\b(?!\.\d)")


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


def _format_version_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the version for report surfaces, flagging a stale worker.

    A failed installed-version probe must stay distinguishable from
    "checked, versions match" — otherwise the exact stale-worker
    condition this field exists to expose disappears whenever the probe
    itself hiccups.
    """
    running = diagnostic_info.get("ha_mcp_version", "Unknown")
    installed = diagnostic_info.get("installed_version")
    if diagnostic_info.get("version_mismatch"):
        return (
            f"{running} (running) — {installed} is installed; "
            "restart to finish applying the update"
        )
    if installed is None:
        return f"{running} (installed-on-disk version could not be verified)"
    return str(running)


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


# Tool-surface-shaping toggles surfaced in bug reports. The set is small on
# purpose: only settings that change which tools the agent sees or whether a
# call runs, since the same bug report behaves very differently depending on
# these. New settings of that kind should be added here so triage doesn't
# have to ask.
#
# ``enable_beta_features`` leads the list because it is the master gate: when
# off it force-disables every beta sub-flag (filesystem tools, code mode, YAML
# editing, ...) regardless of the sub-flag's own value, so a "missing tool"
# report is meaningless without it. ``enable_filesystem_tools`` is a beta-gated
# tool family from issue #1804 — surfacing it lets triage see at a glance whether
# the tool the user couldn't find was even enabled server-side.
# ``read_only_mode`` removes the write tools, and tool security policies can
# hold or refuse a call, so a "write did nothing" report depends on both.
_CONFIG_TOGGLE_FIELDS: tuple[str, ...] = (
    "enable_beta_features",
    "read_only_mode",
    "enable_tool_security_policies",
    "enable_websocket",
    "enable_dashboard_partial_tools",
    "enable_tool_search",
    "tool_search_max_results",
    "enable_yaml_config_editing",
    "enable_filesystem_tools",
    "enable_code_mode",
    "enabled_tool_modules",
)


def _get_config_toggles(settings: Settings | None = None) -> dict[str, Any]:
    """Read tool-surface-shaping config toggles from Settings.

    Defaults to the global settings singleton; tests can pass a fake Settings
    instance instead. Returns an empty dict on any failure (Settings
    construction, attribute coercion, list-field split) so a misconfigured
    environment can't break the bug report path itself.
    """
    try:
        s = settings if settings is not None else get_global_settings()

        toggles: dict[str, Any] = {}
        for field in _CONFIG_TOGGLE_FIELDS:
            value = getattr(s, field, None)
            if value is None:
                continue
            toggles[field] = value

        # Summarize list-shaped seeds as counts rather than dumping the full
        # strings — they can be very long, and listing the exact tools the
        # user disabled isn't useful for triage.
        for list_field in ("disabled_tools", "pinned_tools"):
            raw = getattr(s, list_field, "") or ""
            count = len([item for item in raw.split(",") if item.strip()])
            toggles[f"{list_field}_count"] = count

        return toggles
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


def _format_client_info_for_template(info: dict[str, str]) -> str:
    """Render the MCP client identification as a single human-readable line.

    Falls back to ``unknown (client did not advertise itself)`` when no
    client info was available — this happens for direct MCP clients that
    skip the optional ``clientInfo`` field, or when the bug report tool
    runs outside a live request. Phrasing is deliberately observable
    rather than naming the underlying API field (which may be renamed).
    """
    if not info:
        return "unknown (client did not advertise itself)"
    name = info.get("name") or "unknown"
    version = info.get("version") or "unknown"
    title = info.get("title") or ""
    base = f"{name} {version}"
    if title and title != name:
        return f"{base} _(advertised title: {title})_"
    return base


# Claude Desktop advertises every stdio server as ``local-agent-mode-<server
# name> 1.0.0`` (#1701, #2472, #2484), so the name alone never says which
# Desktop release is involved.
_CLAUDE_DESKTOP_STDIO_PREFIX = "local-agent-mode-"
HOST_NOT_DETECTED = "not detected"

# stdio-to-HTTP bridges present their own identity in the handshake, so the
# server never sees the real client behind them. ``mcp 0.1.0`` is the Python
# MCP SDK's default clientInfo, which is what fastmcp-remote and mcp-proxy
# style bridges send (observed live from Claude Desktop -> fastmcp-remote ->
# component, 2026-09).
_STDIO_BRIDGE_NAMES = {
    "mcp": "Python MCP SDK default identity, i.e. a fastmcp-remote / mcp-proxy style bridge",
    "mcp-remote": "mcp-remote bridge",
    "mcp-proxy": "mcp-proxy bridge",
    "fastmcp-remote": "fastmcp-remote bridge",
}


def _http_user_agent() -> str:
    """The request's ``User-Agent``, or ``""`` outside an HTTP request."""
    value = get_http_headers(include={"user-agent"}).get("user-agent", "")
    return str(value).strip()


def _format_client_host_for_template(diagnostic_info: dict[str, Any]) -> str:
    """Render what the server could learn about the host app beyond ``clientInfo``.

    Over stdio the host is the process that spawned ha-mcp, so the parent
    chain names it and, for Claude Desktop, gives the release. Over HTTP the
    ``User-Agent`` is the only extra signal (and for Anthropic's connector
    broker it carries no app version). The wording tells the agent exactly
    when it still has to ask the user.
    """
    client_info = diagnostic_info.get("mcp_client_info") or {}
    client_host = diagnostic_info.get("mcp_client_host") or {}
    user_agent = diagnostic_info.get("http_user_agent") or ""
    parts: list[str] = []
    name = client_info.get("name") or ""
    if name.startswith(_CLAUDE_DESKTOP_STDIO_PREFIX):
        parts.append("Claude Desktop (local agent mode)")
    # "mcp" is only the SDK default when paired with its literal 0.1.0; a
    # client that names itself "mcp" with a real version is not a bridge.
    bridge = _STDIO_BRIDGE_NAMES.get(name.lower())
    if name.lower() == "mcp" and client_info.get("version") != "0.1.0":
        bridge = None
    if bridge:
        parts.append(
            f"stdio bridge ({bridge}); the real client app is hidden behind "
            "it, ask the user which app and version launched the bridge, do "
            "not guess"
        )
    if diagnostic_info.get("mcp_transport") == "stdio":
        if client_host:
            version = client_host.get("version") or "unknown"
            parts.append(
                f"{client_host.get('name') or 'unknown'} {version} "
                "_(from the parent process)_"
            )
        else:
            parts.append(HOST_NOT_DETECTED)
    elif user_agent and client_info.get("title") != "from HTTP User-Agent":
        parts.append(f"User-Agent `{user_agent}`")
    return "; ".join(parts) or HOST_NOT_DETECTED


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


def _sanitize_log_text(text: str) -> str:
    """Best-effort secret scrubber for log text.

    Defense-in-depth, not exhaustive — bug reports still pass through human
    review (see ``_generate_anonymization_guide``). Rules cover the most common
    leak shapes seen in HA add-on logs:
    JWTs, bearer tokens, long hex tokens, ``key=value`` style credentials,
    URL userinfo, IPv4 addresses with network context, and the MCP connect-URL
    secret path (the add-on's LAN auth).
    """
    # JWT tokens (header.payload.signature)
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[REDACTED_JWT]",
        text,
    )
    # Bearer tokens — match any casing (BEARER, Bearer, bearer, BeArEr, …)
    # via re.IGNORECASE, but preserve the original casing in the output by
    # echoing m.group(1) back through the lambda.
    text = re.sub(
        r"\b(bearer)\s+\S+",
        lambda m: f"{m.group(1)} [REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # Authorization values in any other scheme (Basic, Digest, ...). The
    # Bearer rule above has already handled Bearer, so it is skipped here.
    text = re.sub(
        r"\b(authorization['\"]?\s*[:=]\s*['\"]?)(?!bearer\b)(?:[A-Za-z]+\s+)?[^\s'\",}]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # Generic key=value credentials (api_key, token, secret, password, etc.).
    # Negative lookbehind for a letter so OPENAI_API_KEY=... still matches
    # (underscore is a word-char, so \b doesn't fire there).
    # "authorization" has its own rule above.
    # A quoted key and value, as in JSON or a Python dict repr, are covered
    # too: {"token": "a b"} and {'password': 'x'}.
    text = re.sub(
        r"(?<![A-Za-z])(api[_-]?key|access[_-]?key|secret[_-]?key|token|secret|password|passwd)\b"
        r"(['\"]?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        r"\1\2[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # URL userinfo: scheme://user:password@host -> scheme://user:[REDACTED]@host
    text = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^:/?#\s@]+):([^@/\s]+)@",
        r"\1\2:[REDACTED]@",
        text,
    )
    # Long hex strings (API keys, tokens) - 32+ contiguous hex chars
    text = re.sub(
        r"(?<![a-fA-F0-9])[a-fA-F0-9]{32,}(?![a-fA-F0-9])",
        "[REDACTED_HEX]",
        text,
    )
    # IPv4 addresses — only when there's strong network context, so that
    # four-segment version strings (e.g. "version 1.2.3.4") survive intact.
    text = _IPV4_WITH_PORT_OR_CIDR_RE.sub("[IP]", text)
    text = _IPV4_IN_URL_RE.sub(r"\1[IP]", text)
    text = _IPV4_AFTER_KEYWORD_RE.sub(r"\1[IP]", text)
    # MCP connect-URL secret path — the add-on's LAN auth. Redact by the
    # configured value (catches custom paths) and the generated
    # ``/private_<token>`` convention. Only bug-report output is scrubbed here;
    # the raw logs this text is copied from are left intact.
    # The dedicated settings-UI secret path (OAuth/OIDC) is a second secret whose
    # leak grants the same unauthenticated settings access, so redact a custom
    # value the same way. The auto-generated /private_<token> form is caught by
    # the generic pattern below.
    for env_name in ("MCP_SECRET_PATH", "MCP_SETTINGS_SECRET_PATH"):
        # Strip before rstrip: the resolver strips MCP_SETTINGS_SECRET_PATH before
        # mounting, so a whitespace-bearing value mounts at the stripped path — the
        # redaction pattern must match that, not the raw value (GHSA-mx64-982r-65vg).
        configured = os.getenv(env_name, "").strip().rstrip("/")
        if configured and configured != "/mcp":
            # Anchor on a path-segment boundary so a short/substring-prone
            # configured value (e.g. "/ha") cannot corrupt unrelated text
            # (e.g. "/happy").
            text = re.sub(
                re.escape(configured) + r"(?![A-Za-z0-9_-])",
                "[REDACTED_SECRET_PATH]",
                text,
            )
    text = re.sub(r"/private_[A-Za-z0-9_-]+", "[REDACTED_SECRET_PATH]", text)
    return text


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


def _format_tools_entry_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the File & YAML Tools entry status for the report templates."""
    return diagnostic_info.get("tools_entry_status") or "unknown (probe failed)"


def _format_server_entry_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the in-process server entry status for the report templates."""
    return diagnostic_info.get("server_entry_status") or "unknown (probe failed)"


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
            Literal["runtime_bug", "agent_behavior"],
            Field(
                default="runtime_bug",
                description=(
                    "'runtime_bug' when ha-mcp errored or behaved wrongly; "
                    "'agent_behavior' when the user says you used the wrong "
                    "tool or worked inefficiently."
                ),
            ),
        ] = "runtime_bug",
        title: Annotated[
            str | None,
            Field(
                default=None,
                description="One-line summary of what broke, for the issue title.",
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "What went wrong in markdown: steps to reproduce, expected "
                    "and actual behavior. For agent_behavior: what you did and "
                    "what you should have done."
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
        """Get diagnostics and a finished GitHub issue for a bug report or agent feedback.

        Use it when the user reports an ha-mcp error, failure or wrong result,
        or says you used the wrong tool or worked inefficiently. If it is
        unclear which, ask: "Are you reporting a bug in ha-mcp, or providing
        feedback on how I used the tools?"

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

        # Without the agent's title, the one generated from the last error
        # keeps reports from being filed as a bare "[BUG]".
        suggested_title = _generate_bug_title(diagnostic_info, recent_logs)
        issue_title = _issue_title(report_type, text, suggested_title)

        def build_body(include_logs: bool, text_cap: int | None = None) -> str:
            if report_type == "agent_behavior":
                return _generate_agent_behavior_template(
                    diagnostic_info,
                    log_summary,
                    text=text,
                    include_logs=include_logs,
                    text_cap=text_cap,
                )
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

        issue_body = build_body(include_logs=True)
        issue_url = _build_issue_url(
            issue_title, lambda cap: build_body(include_logs=False, text_cap=cap)
        )

        # Generate search keywords and URLs for duplicate check
        search_keywords = _generate_search_keywords(diagnostic_info, recent_logs)
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
                "inefficiently. If unclear, ASK: 'Are you reporting a bug in "
                "ha-mcp, or providing feedback on how I used the tools?'\n"
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
                "the first way you can:**\n"
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


def _format_config_toggles_for_template(toggles: dict[str, Any]) -> str:
    """Render config toggle snapshot as a markdown bullet list.

    Returns a placeholder line when no toggles were collected (e.g. Settings
    construction failed) so the template stays consistent.
    """
    if not toggles:
        return "_(config toggles unavailable)_"
    lines = []
    for key, value in toggles.items():
        lines.append(f"- **{key}:** `{value}`")
    return "\n".join(lines)


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


def _extract_error_messages(logs: list[dict[str, Any]]) -> list[str]:
    """
    Extract error messages from tool call logs.

    Returns a list of error messages with context (tool name, timestamp).
    """
    if not logs:
        return []

    error_messages = []
    for log in logs:
        error = log.get("error_message")
        if error:
            timestamp = log.get("timestamp", "?")[:19]  # Trim to seconds
            tool_name = log.get("tool_name", "unknown")
            # Format: [timestamp] tool_name: error_message
            # Sanitized here: these lines also go into the pre-filled link,
            # which the agent cannot edit.
            error_messages.append(
                f"[{timestamp}] {tool_name}: {_sanitize_log_text(str(error))}"
            )

    return error_messages


def _generate_bug_title(
    diagnostic_info: dict[str, Any],
    recent_logs: list[dict[str, Any]],
) -> str:
    """
    Generate a concise bug title (single line, ~60 chars max).

    Strategy:
    1. If there are error messages, use the most recent one as basis
    2. Otherwise, use generic template based on connection status
    3. Truncate to ~60 chars max
    """
    title = ""
    # Try to get the most recent error directly from logs
    for log in reversed(recent_logs):
        error_msg = log.get("error_message")
        if error_msg:
            tool_name = log.get("tool_name", "unknown")
            message = " ".join(
                _sanitize_log_text(extract_tool_error_message(error_msg)).split()
            )
            title = f"{tool_name}: {message}"
            break

    if not title:
        # No errors - check connection status
        conn_status = diagnostic_info.get("connection_status", "Unknown")
        if "Error" in conn_status or "Failed" in conn_status:
            title = f"Connection issue: {conn_status}"
        else:
            title = "Issue with ha-mcp"

    # Truncate to ~60 chars, trying to preserve words
    if len(title) > 60:
        title = title[:57] + "..."

    return title


def _generate_search_keywords(
    diagnostic_info: dict[str, Any],
    recent_logs: list[dict[str, Any]],
) -> list[str]:
    """
    Generate search keywords for duplicate issue detection.

    Returns a list of keywords to search for similar issues.
    """
    keywords = set()

    # Find the most recent error from logs
    last_error_log = next(
        (log for log in reversed(recent_logs) if log.get("error_message")), None
    )

    if last_error_log:
        tool_name = last_error_log.get("tool_name")
        if tool_name:
            keywords.add(tool_name)

        error_msg = last_error_log.get("error_message", "").lower()
        # Common error patterns
        if "connection" in error_msg:
            keywords.add("connection")
        if "timeout" in error_msg:
            keywords.add("timeout")
        if "authentication" in error_msg or "auth" in error_msg:
            keywords.add("authentication")
        if "not found" in error_msg:
            keywords.add("not found")

    # Add connection-based keywords
    conn_status = diagnostic_info.get("connection_status", "Unknown")
    if "Error" in conn_status or "Failed" in conn_status:
        keywords.add("connection")

    # Default to generic search if no specific keywords
    if not keywords:
        keywords.add("bug")

    return list(keywords)


@dataclass(frozen=True)
class _ReportText:
    """The parts of a report only the agent can write. Any may be missing."""

    title: str | None = None
    description: str | None = None
    user_prompt: str | None = None
    tool_calls: str | None = None
    user_comment: str | None = None
    ai_model: str | None = None
    client_app: str | None = None


_FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

_LOGS_LEFT_OUT = """## 📊 Logs

The logs are not in this pre-filled link, which has to stay short. Paste them
here from the full report in the chat.
"""


def _fenced(text: str) -> str:
    """Wrap ``text`` in a tilde code fence that nothing inside can close.

    Agents show the report inside a backtick code block, and a tilde line
    never closes a backtick fence, so the report stays in one piece. The
    fence is longer than any tilde run in ``text``, so a log or tool call
    with its own tilde fence stays inside.
    """
    longest = max((len(run) for run in re.findall(r"~+", text)), default=0)
    fence = "~" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _shorten(value: str | None, cap: int | None) -> str | None:
    """Cut ``value`` to ``cap`` characters and say so; None means no cap."""
    if value is None or cap is None or len(value) <= cap:
        return value
    cut = _close_open_block(value[:cap])
    return f"{cut}\n… (cut to fit the link; the full text is in the chat)"


def _shorten_text(text: _ReportText, cap: int | None) -> _ReportText:
    """Cut every part of the agent's text except the title to ``cap``."""
    return replace(
        text,
        description=_shorten(text.description, cap),
        user_prompt=_shorten(text.user_prompt, cap),
        tool_calls=_shorten(text.tool_calls, cap),
        user_comment=_shorten(text.user_comment, cap),
        ai_model=_shorten(text.ai_model, cap),
        client_app=_shorten(text.client_app, cap),
    )


def _close_open_block(text: str) -> str:
    """Close a code fence or HTML comment that a cut left open.

    Either one left open hides or swallows everything after it, including
    the note that says the text was cut. ``<!--`` inside a code fence is
    text, not a comment, so the scan tracks which block it is in.
    """
    open_fence: str | None = None
    in_comment = False
    for line in text.split("\n"):
        match = _FENCE_LINE_RE.match(line)
        if open_fence is not None:
            if match is not None:
                run, rest = match.groups()
                if (
                    run[0] == open_fence[0]
                    and len(run) >= len(open_fence)
                    and not rest.strip()
                ):
                    open_fence = None
            continue
        if not in_comment and match is not None:
            open_fence = match.group(1)
            continue
        pos = 0
        while True:
            marker = "-->" if in_comment else "<!--"
            found = line.find(marker, pos)
            if found < 0:
                break
            in_comment, pos = not in_comment, found + len(marker)
    if open_fence is not None:
        return f"{text}\n{open_fence}"
    if in_comment:
        return f"{text} -->"
    return text


def _format_supervisor_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the Supervisor probe, keeping a failed probe visible."""
    info = diagnostic_info.get("supervisor") or {"error": "not probed"}
    if "none" in info:
        return "none (Home Assistant runs without a Supervisor)"
    if "error" in info:
        return f"probe failed ({info['error']})"
    return f"{info['supervisor_version']} (host OS: {info['host_os']})"


def _format_client_host_line(diagnostic_info: dict[str, Any], text: _ReportText) -> str:
    """Render the client host row, preferring the user's own answer.

    The auto-detected value can carry a hint for the agent to ask the user,
    which means nothing to a reader once the user has answered.
    """
    if text.client_app:
        return f"{text.client_app} _(reported by the user)_"
    return f"{_format_client_host_for_template(diagnostic_info)} _(auto-detected)_"


def _user_comment_section(text: _ReportText) -> str:
    """Render the reporter's own words, which the issue forms always required."""
    comment = text.user_comment or (
        "<fill in: ask the user to describe the problem in their own words>"
    )
    return f"## 🗣️ In the Reporter's Words\n\n{comment}\n\n"


def _tool_call_section(text: _ReportText, heading_note: str) -> str:
    """Render the triggering prompt and tool call, or placeholders for them."""
    if text.user_prompt:
        prompt = f"**User prompt:**\n\n{_fenced(text.user_prompt)}"
    else:
        prompt = "**User prompt:** <fill in>"
    calls = _fenced(
        text.tool_calls
        or "<fill in: name + arguments + (truncated) response, e.g.:\n"
        'ha_call_service(domain="light", service="turn_on", '
        'entity_id="light.example")\n'
        "→ ToolError: Service not found\n>"
    )
    return f"""## 💬 Triggering Prompt & Tool Call

{prompt}

**{heading_note}**
{calls}
"""


def _generate_runtime_bug_template(
    diagnostic_info: dict[str, Any],
    log_summary: str,
    startup_log_summary: str,
    recent_logs: list[dict[str, Any]],
    startup_logs: list[dict[str, Any]],
    *,
    addon_logs: str = "",
    core_error_log: str = "",
    text: _ReportText = _ReportText(),
    include_logs: bool = True,
    text_cap: int | None = None,
) -> str:
    """Build the runtime bug report as a GitHub issue body.

    ``text`` fills the parts only the agent knows; without it they stay as
    placeholders. The pre-filled link has to stay short, so for it
    ``include_logs=False`` leaves the log sections out and ``text_cap`` cuts
    every part of ``text`` except the title, and the error messages, to that
    many characters each.
    """
    text = _shorten_text(text, text_cap)
    platform_info = diagnostic_info.get("platform", {})
    config_toggles = diagnostic_info.get("config_toggles") or {}
    mcp_transport = diagnostic_info.get("mcp_transport", "unknown")
    client_info = diagnostic_info.get("mcp_client_info") or {}

    error_messages = _extract_error_messages(recent_logs)
    error_section = _shorten(
        "\n".join(error_messages)
        if error_messages
        else "No errors detected in recent logs",
        text_cap,
    )

    config_toggles_section = (
        f"{_format_config_toggles_for_template(config_toggles)}\n"
        f"- **tool_policy:** `{diagnostic_info.get('tool_policy', 'not probed')}`"
    )

    if text.description:
        description_section = f"## 📋 Bug Description\n\n{text.description}\n"
    else:
        description_section = """## 📋 Bug Description
<!-- ONE clear sentence: What went wrong? -->


## 🔄 Steps to Reproduce
1.
2.
3.

## ✅ Expected vs ❌ Actual Behavior

**Expected:**
<!-- What should have happened? -->


**Actual:**
<!-- What actually happened? -->
"""

    if not include_logs:
        log_sections = f"\n---\n\n{_LOGS_LEFT_OUT}"
    else:
        log_sections = f"""
---

## 📊 Recent Tool Calls

<details>
<summary>Click to expand recent tool calls (auto-filled by ha_report_issue)</summary>

{_fenced(log_summary)}

</details>
"""
        if startup_logs:
            log_sections += f"""
---

## 🚀 Startup Logs (if relevant)

<details>
<summary>Click to expand startup logs</summary>

{_fenced(startup_log_summary)}

</details>
"""
        # Add-on installs only.
        if addon_logs:
            log_sections += f"""
---

## 📦 Add-on Container Logs

<details>
<summary>Click to expand ha-mcp add-on logs</summary>

{_fenced(addon_logs)}

</details>
"""
        # All install types. This carries the auth / integration errors that
        # diagnose issues like #1694 and don't appear in the add-on log above.
        if core_error_log:
            log_sections += f"""
---

## Home Assistant Error Log

<details>
<summary>Click to expand home-assistant.log (auth / integration errors)</summary>

{_fenced(core_error_log)}

</details>
"""

    return f"""## 🚨 Auto-Generated by `ha_report_issue` Tool

> This report was generated by the ha_report_issue tool.
> Environment info and logs were collected automatically.

---

{_user_comment_section(text)}{description_section}
---

{_tool_call_section(text, "Tool call(s):")}
---

## 🔧 Environment

- **ha-mcp Version:** {_format_version_value(diagnostic_info)}
- **Custom Component:** {diagnostic_info.get("component_version") or "not detected (not installed, or probe failed)"}
- **File & YAML Tools entry:** {_format_tools_entry_value(diagnostic_info)}
- **In-process Server entry:** {_format_server_entry_value(diagnostic_info)}
- **Installation Method:** {diagnostic_info.get("installation_method", "Unknown")}
- **MCP Transport:** {mcp_transport} _(auto-detected — correct if wrong)_
- **MCP Client:** {_format_client_info_for_template(client_info)} _(auto-detected from the MCP `initialize` handshake)_
- **MCP Client Host:** {_format_client_host_line(diagnostic_info, text)}
- **AI Model:** {text.ai_model or ""}
- **Operating System:** {platform_info.get("os", "Unknown")} {platform_info.get("os_release", "")} ({platform_info.get("architecture", "Unknown")})
- **Python Version:** {platform_info.get("python_version", "Unknown")}
- **Home Assistant Version:** {diagnostic_info.get("home_assistant_version", "Unknown")}
- **Supervisor:** {_format_supervisor_value(diagnostic_info)}
- **Connection Status:** {diagnostic_info.get("connection_status", "Unknown")}
- **Entity Count:** {diagnostic_info.get("entity_count", 0)}

---

## ⚙️ ha-mcp Configuration

These settings shape which tools the agent sees and whether a call runs, so
the same report can mean different things depending on them. Auto-collected
from the running server:

{config_toggles_section}

---

## 🚨 Error Messages

{_fenced(error_section or "")}
{log_sections}
---

## 💡 Additional Context

<!-- Any other relevant information: -->
<!-- - Suggested fixes -->
<!-- - Workarounds you found -->
<!-- - Related issues -->
<!-- - Configuration snippets -->


---

**Privacy reminder:** Please review and anonymize sensitive information (tokens, IPs, personal names) before submitting.

{REPORT_END_MARKER}
"""


def _generate_agent_behavior_template(
    diagnostic_info: dict[str, Any],
    log_summary: str,
    *,
    text: _ReportText = _ReportText(),
    include_logs: bool = True,
    text_cap: int | None = None,
) -> str:
    """Build the agent behavior feedback as a GitHub issue body.

    ``text``, ``include_logs`` and ``text_cap`` work as in the runtime bug
    template.
    """
    text = _shorten_text(text, text_cap)
    config_toggles = diagnostic_info.get("config_toggles") or {}
    mcp_transport = diagnostic_info.get("mcp_transport", "unknown")
    client_info = diagnostic_info.get("mcp_client_info") or {}
    config_toggles_section = (
        f"{_format_config_toggles_for_template(config_toggles)}\n"
        f"- **tool_policy:** `{diagnostic_info.get('tool_policy', 'not probed')}`"
    )

    if text.description:
        description_section = f"## 🤖 What Happened\n\n{text.description}\n"
    else:
        description_section = """## 🤖 What Did the AI Agent Do?

<!-- Describe what the AI agent did that could be improved -->
<!-- Examples: -->
<!-- - Used the wrong tool initially, then corrected itself -->
<!-- - Provided invalid parameters to a tool -->
<!-- - Made multiple unnecessary tool calls -->
<!-- - Missed an obvious shortcut or better approach -->
<!-- - Misinterpreted tool output -->


## 🎯 What Should the Agent Have Done?

<!-- Describe the more efficient or correct approach -->


## 📝 Conversation Context

<!-- Provide context about what you were trying to do -->
<!-- Example: "I asked the agent to create an automation that..." -->
"""

    if include_logs:
        log_section = f"""## 🔧 Tool Calls Made (Auto-Filled)

<details>
<summary>Click to expand tool call sequence</summary>

{_fenced(log_summary)}

</details>
"""
    else:
        log_section = _LOGS_LEFT_OUT

    return f"""## 🤖 Auto-Generated by `ha_report_issue` Tool

> This report was generated by the ha_report_issue tool.
> Tool call history was collected automatically to help analyze agent behavior.

---

{_user_comment_section(text)}{description_section}
---

{_tool_call_section(text, "Tool call(s) the agent chose:")}
---

{log_section}
---

## 💡 Suggested Improvement

<!-- How could the agent be improved? Options: -->

- [ ] **Tool documentation** - Tool description or examples need clarification
- [ ] **Error messages** - Tool should return better guidance on failure
- [ ] **Tool design** - Tool should accept different parameters or return more info
- [ ] **Agent prompting** - System prompt should guide agent differently
- [ ] **New tool needed** - Missing functionality requires a new tool
- [ ] **Other** - Describe below

**Details:**
<!-- Explain your suggestion -->


---

## 📊 Environment

- **ha-mcp Version:** {_format_version_value(diagnostic_info)}
- **Custom Component:** {diagnostic_info.get("component_version") or "not detected (not installed, or probe failed)"}
- **File & YAML Tools entry:** {_format_tools_entry_value(diagnostic_info)}
- **In-process Server entry:** {_format_server_entry_value(diagnostic_info)}
- **Installation Method:** {diagnostic_info.get("installation_method", "Unknown")}
- **MCP Transport:** {mcp_transport} _(auto-detected — correct if wrong)_
- **MCP Client:** {_format_client_info_for_template(client_info)} _(auto-detected from the MCP `initialize` handshake)_
- **MCP Client Host:** {_format_client_host_line(diagnostic_info, text)}
- **AI Model:** {text.ai_model or ""}
- **Home Assistant Version:** {diagnostic_info.get("home_assistant_version", "Unknown")}
- **Supervisor:** {_format_supervisor_value(diagnostic_info)}

---

## ⚙️ ha-mcp Configuration

These settings shape which tools the agent sees and whether a call runs, so
the same behavior may be expected or surprising depending on them:

{config_toggles_section}

---

## 📎 Additional Context

<!-- Screenshots, conversation logs, or other helpful info -->


---

**Note:** This is for improving AI agent behavior. For ha-mcp bugs (errors, crashes), file a runtime bug report instead.

{REPORT_END_MARKER}
"""


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


def _new_issue_url(title: str, body: str) -> str:
    return f"{NEW_ISSUE_URL}?{urlencode({'title': title, 'body': body})}"


def _build_issue_url(title: str, render: Callable[[int | None], str]) -> str:
    """Return a new-issue link with title and body filled in.

    ``render(cap)`` builds the body with every part of the agent's text
    except the title, and the error messages, cut to ``cap`` characters, or
    uncut for None. Long text is shortened first, so the
    environment block, which triage needs most, stays in the link. Only when
    that is not enough is the body itself cut from the end.
    """
    url = _new_issue_url(title, render(None))
    if len(url) <= _ISSUE_URL_MAX_CHARS:
        return url
    best: str | None = None
    low, high = 0, len(url)
    while low <= high:
        mid = (low + high) // 2
        candidate = _new_issue_url(title, render(mid))
        if len(candidate) <= _ISSUE_URL_MAX_CHARS:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    return best or _cut_to_fit(title, render(0))


def _cut_to_fit(title: str, body: str) -> str:
    """Cut ``body`` at the longest prefix whose link fits, and say so."""
    note = (
        "\n\n_(Cut to fit the link. The full report is in the chat.)_\n\n"
        f"{REPORT_END_MARKER}\n"
    )
    core = body.removesuffix(f"{REPORT_END_MARKER}\n").rstrip()

    def fits(length: int) -> bool:
        cut = _close_open_block(core[:length]) + note
        return len(_new_issue_url(title, cut)) <= _ISSUE_URL_MAX_CHARS

    low, high = 0, len(core)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return _new_issue_url(title, _close_open_block(core[:low]) + note)


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
