"""
Bug report tool for Home Assistant MCP Server.

This module provides a tool to collect diagnostic information and guide users
on how to create effective bug reports.
"""

import logging
import os
import platform
import re
import sys
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote_plus

import httpx
from fastmcp import Context
from pydantic import Field

from ha_mcp import __version__

from .._version import get_supervisor_base_url
from ..config import Settings, get_global_settings
from ..utils.usage_logger import (
    AVG_LOG_ENTRIES_PER_TOOL,
    get_recent_logs,
    get_startup_logs,
)
from .helpers import log_tool_usage
from .util_helpers import ANSI_ESCAPE_RE

logger = logging.getLogger(__name__)

# GitHub issue template URLs
RUNTIME_BUG_URL = (
    "https://github.com/homeassistant-ai/ha-mcp/issues/new?template=runtime_bug.yml"
)
AGENT_BEHAVIOR_URL = (
    "https://github.com/homeassistant-ai/ha-mcp/issues/new?template=agent_behavior.yml"
)

# Max characters to include from addon container logs.
# 3000 chars ≈ 750 LLM tokens — keeps the tool response well below context budgets
# while still capturing enough recent output to diagnose most issues.
_ADDON_LOG_MAX_CHARS = 3000

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

    Returns one of: pyinstaller, addon, docker, git, pypi, unknown
    """
    # 1. PyInstaller binary
    if getattr(sys, "frozen", False):
        return "pyinstaller"

    # 2. Home Assistant Add-on (has supervisor token)
    if os.environ.get("SUPERVISOR_TOKEN"):
        return "addon"

    # 3. Docker container (non-addon)
    if Path("/.dockerenv").exists():
        return "docker"

    # 4. Git clone - check for .git directory relative to package
    try:
        # Go up from tools_bug_report.py -> tools -> ha_mcp -> src -> project_root
        project_root = Path(__file__).parent.parent.parent.parent
        if (project_root / ".git").exists():
            return "git"
    except Exception:
        pass

    # 5. PyPI install - marker file exists in package
    try:
        marker_path = Path(__file__).parent.parent / "_pypi_marker"
        if marker_path.exists():
            return "pypi"
    except Exception:
        pass

    # 6. Default - unknown
    return "unknown"


def _detect_platform() -> dict[str, str]:
    """Detect platform information."""
    return {
        "os": platform.system(),  # Windows, Darwin, Linux
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }


# Tool-surface-shaping toggles surfaced in bug reports. The set is small on
# purpose: only flags that materially change which tools the agent sees, since
# the same bug report behaves very differently depending on these. New
# tool-shaping toggles should be added here so triage doesn't have to ask.
_CONFIG_TOGGLE_FIELDS: tuple[str, ...] = (
    "enable_websocket",
    "enable_dashboard_partial_tools",
    "enable_tool_search",
    "tool_search_max_results",
    "enable_yaml_config_editing",
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
    except Exception as e:
        logger.warning(
            "Failed to read settings for bug report toggles: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


def _extract_client_info(ctx: Context | None) -> dict[str, str]:
    """Pull the connecting MCP client's self-identification off the request context.

    The MCP ``initialize`` handshake carries a ``clientInfo`` Implementation
    object (``name``/``version``/optional ``title``). FastMCP exposes the
    underlying server session as ``ctx.session``; the MCP SDK's
    ``ServerSession`` keeps the parsed initialize params on ``client_params``.
    The attribute name on the parsed Pydantic model is ``clientInfo`` in
    ``mcp`` 1.24.x (the version this project pins) — we also fall back to
    ``client_info`` to stay forward-compatible with SDK versions that switch
    to snake_case.

    Returns ``{"name": ..., "version": ..., "title": ...}``. ``name`` and
    ``version`` fall back to ``"unknown"`` when the client didn't send them;
    ``title`` falls back to the empty string so callers can distinguish "not
    sent" from a real title without false-positive aside rendering.

    Returns an empty dict if no context is available (tool invoked outside an
    MCP request, e.g. unit tests) so the bug-report path stays robust. The
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
        if params is None:
            return {}
        # Try the camelCase attribute (mcp 1.24.x) first, then snake_case so
        # we keep working if the SDK switches the alias direction.
        client = getattr(params, "clientInfo", None) or getattr(
            params, "client_info", None
        )
        if client is None:
            return {}
        return {
            "name": getattr(client, "name", None) or "unknown",
            "version": getattr(client, "version", None) or "unknown",
            "title": getattr(client, "title", None) or "",
        }
    except Exception as e:
        logger.info(
            "Failed to read MCP client info from context: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


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


def _detect_mcp_transport() -> str:
    """Best-effort MCP transport detection.

    Returns ``stdio`` / ``http`` / ``sse`` / ``unknown``. We can't observe the
    transport perfectly from a tool call, so we look at the entrypoint name
    and well-known env hints. The result is informational — the bug template
    surfaces it as an auto-detect that the agent or user can override.
    """
    # Entry-point script name (e.g. ``ha-mcp-web`` for HTTP, ``ha-mcp-sse``
    # for SSE; pyproject.toml's [project.scripts] is the source of truth).
    argv0 = (sys.argv[0] if sys.argv else "").lower()
    basename = os.path.basename(argv0)
    if basename.endswith("-web"):
        return "http"
    if basename.endswith("-sse"):
        return "sse"

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
    URL userinfo, and IPv4 addresses with network context.
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
    # Generic key=value credentials (api_key, token, secret, password, etc.).
    # Negative lookbehind for a letter so OPENAI_API_KEY=... still matches
    # (underscore is a word-char, so \b doesn't fire there).
    # "authorization" is intentionally omitted — the Bearer rule above already
    # handles "Authorization: Bearer ..." and overlapping rules double-tap.
    text = re.sub(
        r"(?<![A-Za-z])(api[_-]?key|access[_-]?key|secret[_-]?key|token|secret|password|passwd)\b(\s*[:=]\s*)\S+",
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
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return ""

    try:
        async with httpx.AsyncClient(
            timeout=10.0, verify=get_global_settings().verify_ssl
        ) as http_client:
            resp = await http_client.get(
                f"{get_supervisor_base_url()}/addons/self/logs",
                headers={"Authorization": f"Bearer {token}"},
            )
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


def register_bug_report_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register bug report tools with the MCP server."""

    @mcp.tool(
        tags={"Utilities"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Report Issue or Feedback",
        },
    )
    @log_tool_usage
    async def ha_report_issue(
        tool_call_count: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                le=16,
                description=(
                    "Number of tool calls made since the issue started. "
                    "This determines how many log entries to include. "
                    "Count how many ha_* tools were called from when the issue began. "
                    "Default: 10. Max: 16 (limited by 200-entry log buffer: 16*4*3=192)"
                ),
            ),
        ] = 10,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Collect diagnostic information for filing issue reports or feedback.

        This tool generates templates for TWO types of reports:
        1. **Runtime Bug Report** - For ha-mcp errors, failures, unexpected behavior
        2. **Agent Behavior Feedback** - For AI agent inefficiency, wrong tool usage

        **IMPORTANT FOR AI AGENTS:**
        You MUST analyze the conversation context to determine which template to present:

        🐛 **Present RUNTIME BUG template if:**
           - User reports an error, failure, or unexpected behavior
           - A tool returned an error or incorrect result
           - Something is broken or not working in ha-mcp

        🤖 **Present AGENT BEHAVIOR template if:**
           - User mentions YOU (the agent) used the wrong tool
           - User suggests a more efficient workflow
           - User reports YOUR inefficiency or mistakes
           - User says you should have done something differently

        **If unclear which type, ASK the user:**
        "Are you reporting a bug in ha-mcp, or providing feedback on how I used the tools?"

        **WHEN TO USE THIS TOOL:**
        - "I want to file a bug/issue/report"
        - "This isn't working"
        - "You should have used [other tool]"
        - "That was inefficient"

        **OUTPUT:**
        Returns both templates plus diagnostic data. Key fields:
        - `runtime_bug_template`, `agent_behavior_template` — pick based on context
        - `recent_logs`, `startup_logs` — captured ha-mcp tool/server log entries
        - `addon_logs` — addon container stdout/stderr (HA add-on installs only;
          empty string otherwise)
        - `suggested_title`, `duplicate_check_urls`, `anonymization_guide`
        """
        # Detect installation method, platform, and runtime config.
        install_method = _detect_installation_method()
        platform_info = _detect_platform()
        config_toggles = _get_config_toggles()
        mcp_transport = _detect_mcp_transport()
        client_info = _extract_client_info(ctx)

        diagnostic_info: dict[str, Any] = {
            "ha_mcp_version": __version__,
            "installation_method": install_method,
            "platform": platform_info,
            "mcp_transport": mcp_transport,
            "mcp_client_info": client_info,
            "config_toggles": config_toggles,
            "connection_status": "Unknown",
            "home_assistant_version": "Unknown",
            "entity_count": 0,
        }

        # Try to get Home Assistant config and connection status
        try:
            config = await client.get_config()
            diagnostic_info["connection_status"] = "Connected"
            diagnostic_info["home_assistant_version"] = config.get("version", "Unknown")
            diagnostic_info["location_name"] = config.get("location_name", "Unknown")
            diagnostic_info["time_zone"] = config.get("time_zone", "Unknown")
        except Exception as e:
            logger.warning(f"Failed to get Home Assistant config: {e}")
            diagnostic_info["connection_status"] = f"Connection Error: {str(e)}"

        # Try to get entity count
        try:
            states = await client.get_states()
            if states:
                diagnostic_info["entity_count"] = len(states)
        except Exception as e:
            logger.warning(f"Failed to get entity count: {e}")

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

        # Format logs for inclusion (sanitized summary)
        log_summary = _format_logs_for_report(recent_logs)
        startup_log_summary = _format_startup_logs(startup_logs)

        # Build the formatted report
        report_lines = [
            "=== ha-mcp Bug Report Info ===",
            "",
            f"ha-mcp Version: {diagnostic_info['ha_mcp_version']}",
            f"Installation Method: {diagnostic_info['installation_method']}",
            f"MCP Transport: {mcp_transport}",
            f"MCP Client: {_format_client_info_for_template(client_info)}",
            f"Operating System: {platform_info['os']} {platform_info['os_release']} ({platform_info['architecture']})",
            f"Python Version: {platform_info['python_version']}",
            f"Home Assistant Version: {diagnostic_info['home_assistant_version']}",
            f"Connection Status: {diagnostic_info['connection_status']}",
            f"Entity Count: {diagnostic_info['entity_count']}",
        ]

        # Add optional fields if available
        if "location_name" in diagnostic_info:
            report_lines.append(f"Location Name: {diagnostic_info['location_name']}")
        if "time_zone" in diagnostic_info:
            report_lines.append(f"Time Zone: {diagnostic_info['time_zone']}")

        if config_toggles:
            report_lines.extend(["", "=== ha-mcp Config Toggles ==="])
            for key, value in config_toggles.items():
                report_lines.append(f"  {key}: {value}")

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
            report_lines.extend(
                [
                    "",
                    "=== Add-on Container Logs ===",
                    addon_logs,
                ]
            )

        formatted_report = "\n".join(report_lines)

        # Generate suggested title up-front so it can be folded into the
        # submission URLs as a `&title=` query param. This auto-fills the
        # GitHub issue title field — without it, users routinely submit reports
        # titled just "[BUG]".
        suggested_title = _generate_bug_title(diagnostic_info, recent_logs)
        title_query = quote_plus(suggested_title)
        runtime_bug_submit_url = f"{RUNTIME_BUG_URL}&title={title_query}"
        agent_behavior_submit_url = f"{AGENT_BEHAVIOR_URL}&title={title_query}"

        # Generate BOTH templates
        runtime_bug_template = _generate_runtime_bug_template(
            diagnostic_info,
            log_summary,
            startup_log_summary,
            recent_logs,
            startup_logs,
            addon_logs=addon_logs,
            submit_url=runtime_bug_submit_url,
        )

        agent_behavior_template = _generate_agent_behavior_template(
            diagnostic_info,
            log_summary,
            recent_logs,
            submit_url=agent_behavior_submit_url,
        )

        # Anonymization instructions
        anonymization_guide = _generate_anonymization_guide()

        # Generate search keywords and URLs for duplicate check
        search_keywords = _generate_search_keywords(diagnostic_info, recent_logs)
        duplicate_check_urls = [
            f"https://github.com/homeassistant-ai/ha-mcp/issues?q=is%3Aissue+{quote_plus(keyword)}"
            for keyword in search_keywords[:3]  # Limit to top 3 keywords
        ]

        return {
            "success": True,
            "diagnostic_info": diagnostic_info,
            "recent_logs": recent_logs,
            "startup_logs": startup_logs,
            "addon_logs": addon_logs,
            "log_count": len(recent_logs),
            "startup_log_count": len(startup_logs),
            "formatted_report": formatted_report,
            "runtime_bug_template": runtime_bug_template,
            "agent_behavior_template": agent_behavior_template,
            "anonymization_guide": anonymization_guide,
            "suggested_title": suggested_title,
            "runtime_bug_submit_url": runtime_bug_submit_url,
            "agent_behavior_submit_url": agent_behavior_submit_url,
            "duplicate_check_urls": duplicate_check_urls,
            "instructions": (
                "WORKFLOW FOR PRESENTING BUG REPORTS:\n\n"
                "1. **Check for duplicates FIRST** (before presenting the template):\n"
                "   - Use the duplicate_check_urls to search for similar issues\n"
                '   - If gh CLI is available: use `gh issue list --search "keyword"`\n'
                "   - Otherwise: inform user to check the duplicate_check_urls\n"
                "   - If duplicates found, ask user if they want to comment on existing issue instead\n\n"
                "2. **Determine which template to present**:\n"
                "   - ANALYZE THE CONVERSATION to determine which template to present\n\n"
                "   🐛 Present RUNTIME_BUG_TEMPLATE if:\n"
                "      - User reports an error, failure, or unexpected behavior in ha-mcp\n"
                "      - A tool returned an error or incorrect result\n"
                "      - Something is broken or not working\n\n"
                "   🤖 Present AGENT_BEHAVIOR_TEMPLATE if:\n"
                "      - User mentions YOU (the agent) used the wrong tool\n"
                "      - User suggests YOU should have done something differently\n"
                "      - User reports YOUR inefficiency or mistakes\n\n"
                "   If UNCLEAR which type, ASK: 'Are you reporting a bug in ha-mcp, or providing feedback on how I used the tools?'\n\n"
                "3. **ANONYMIZE before presenting** (CRITICAL):\n"
                "   BEFORE showing the report to the user, YOU MUST anonymize sensitive information:\n"
                "   a. Replace person names with generic labels (person.user1, person.user2)\n"
                "   b. Replace location names with generic names (Home, Location1)\n"
                "   c. Replace device names containing personal info (e.g., 'juliens_bedroom') with generic ones (e.g., 'bedroom_1')\n"
                "   d. Verify no tokens, passwords, or IPs are visible\n"
                "   e. Keep entity domains, error messages, and technical details\n"
                "   See anonymization_guide for full details.\n\n"
                "4. **Fill in the self-reported fields BEFORE presenting**:\n"
                "   - `**AI Model:**` — write your identity on this line (provider/family + the\n"
                "     most specific version you know, in whatever form you'd describe yourself).\n"
                "     Do not invent a version number. If you don't know it, say so or omit the\n"
                "     version. There are no options to pick from — just answer honestly.\n"
                "   - `**Triggering Prompt & Tool Call:** <fill in>` — the EXACT user message\n"
                "     and the tool call(s) that produced the bug, copy-pasted verbatim. Truncate\n"
                "     long inputs only after anonymization. This is the single most useful field\n"
                "     for triage — do not skip it.\n"
                "   `MCP Transport` and `MCP Client` are auto-detected by the server (the latter\n"
                "   from the MCP `initialize` handshake); leave both as-is unless they're clearly\n"
                "   wrong.\n\n"
                "5. **Present the anonymized report to the user**:\n"
                "   a. Show the suggested_title (user can edit if needed) and tell them GitHub's\n"
                "      title field is now pre-filled via the submission URL — they don't need to\n"
                "      retype it.\n"
                "   b. Present the chosen ANONYMIZED template IN A MARKDOWN CODE BLOCK (```markdown...```) for easy copy/paste\n"
                "   c. PROMINENTLY display the submission URL at the top — these include the\n"
                "      pre-filled title:\n"
                "      - Runtime bugs: see runtime_bug_submit_url\n"
                "      - Agent behavior: see agent_behavior_submit_url\n"
                "   d. Ask them to fill in the description sections\n"
                "   e. For HA add-on installs, the runtime bug template includes a collapsible '📦 Add-on Container Logs' section auto-filled from addon_logs — keep it as-is\n"
                "   f. Remind them to review for any remaining personal information before submitting\n\n"
                "CRITICAL: Always ANONYMIZE the report BEFORE presenting it in markdown code blocks!"
            ),
        }


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
            error_messages.append(f"[{timestamp}] {tool_name}: {error}")

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
            title = f"{tool_name}: {error_msg}"
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


def _generate_runtime_bug_template(
    diagnostic_info: dict[str, Any],
    log_summary: str,
    startup_log_summary: str,
    recent_logs: list[dict[str, Any]],
    startup_logs: list[dict[str, Any]],
    *,
    addon_logs: str = "",
    submit_url: str = RUNTIME_BUG_URL,
) -> str:
    """
    Generate a runtime bug report template matching runtime_bug.md format.

    This template matches the GitHub issue template EXACTLY so users can
    copy-paste without format conflicts.
    """
    platform_info = diagnostic_info.get("platform", {})
    config_toggles = diagnostic_info.get("config_toggles") or {}
    mcp_transport = diagnostic_info.get("mcp_transport", "unknown")
    client_info = diagnostic_info.get("mcp_client_info") or {}

    # Extract error messages from recent logs
    error_messages = _extract_error_messages(recent_logs)
    error_section = (
        "\n".join(error_messages)
        if error_messages
        else "<!-- No errors detected in recent logs -->"
    )

    config_toggles_section = _format_config_toggles_for_template(config_toggles)

    # Show startup logs section only if they exist
    startup_section = ""
    if startup_logs:
        startup_section = f"""
---

## 🚀 Startup Logs (if relevant)

<details>
<summary>Click to expand startup logs</summary>

```
{startup_log_summary}
```

</details>
"""

    # Show addon container logs section only when available (addon installs only)
    addon_section = ""
    if addon_logs:
        addon_section = f"""
---

## 📦 Add-on Container Logs

<details>
<summary>Click to expand ha-mcp add-on logs</summary>

```
{addon_logs}
```

</details>
"""

    return f"""## 🚨 Auto-Generated by `ha_report_issue` Tool

> This template was auto-generated by the ha_report_issue tool.
> All environment info and logs below were collected automatically.

**Submit this report at:**
{submit_url}

(The submission link above pre-fills the issue title — you don't need to retype it.)

---

## 📋 Bug Description
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


---

## 💬 Triggering Prompt & Tool Call

<!-- The calling AI agent fills this in. Paste, verbatim, the user message that
     triggered this bug AND the tool call(s) that produced it. Truncate only
     after anonymizing tokens / personal names. This is the highest-leverage
     field for triage. -->

**User prompt:** <fill in>

**Tool call(s):**
```
<fill in — name + arguments + (truncated) response, e.g.:
ha_call_service(domain="light", service="turn_on", entity_id="light.example")
→ ToolError: Service not found
>
```

---

## 🔧 Environment

- **ha-mcp Version:** {diagnostic_info.get("ha_mcp_version", "Unknown")}
- **Installation Method:** {diagnostic_info.get("installation_method", "Unknown")}
- **MCP Transport:** {mcp_transport} _(auto-detected — correct if wrong)_
- **MCP Client:** {_format_client_info_for_template(client_info)} _(auto-detected from the MCP `initialize` handshake)_
- **AI Model:**
- **Operating System:** {platform_info.get("os", "Unknown")} {platform_info.get("os_release", "")} ({platform_info.get("architecture", "Unknown")})
- **Python Version:** {platform_info.get("python_version", "Unknown")}
- **Home Assistant Version:** {diagnostic_info.get("home_assistant_version", "Unknown")}
- **Connection Status:** {diagnostic_info.get("connection_status", "Unknown")}
- **Entity Count:** {diagnostic_info.get("entity_count", 0)}

---

## ⚙️ ha-mcp Configuration

These flags shape which tools the agent sees, so the same report can mean
different things depending on toggle state. Auto-collected from the running
server:

{config_toggles_section}

---

## 🚨 Error Messages

```
{error_section}
```

---

## 📊 Recent Tool Calls

<details>
<summary>Click to expand recent tool calls (auto-filled by ha_report_issue)</summary>

```
{log_summary}
```

</details>
{startup_section}{addon_section}
---

## 💡 Additional Context

<!-- Any other relevant information: -->
<!-- - Suggested fixes -->
<!-- - Workarounds you found -->
<!-- - Related issues -->
<!-- - Configuration snippets -->


---

**Privacy reminder:** Please review and anonymize sensitive information (tokens, IPs, personal names) before submitting.
"""


def _generate_agent_behavior_template(
    diagnostic_info: dict[str, Any],
    log_summary: str,
    recent_logs: list[dict[str, Any]],
    *,
    submit_url: str = AGENT_BEHAVIOR_URL,
) -> str:
    """
    Generate an agent behavior feedback template matching agent_behavior_feedback.md format.

    This template focuses on AI agent tool usage patterns and inefficiencies.
    """
    config_toggles = diagnostic_info.get("config_toggles") or {}
    mcp_transport = diagnostic_info.get("mcp_transport", "unknown")
    client_info = diagnostic_info.get("mcp_client_info") or {}
    config_toggles_section = _format_config_toggles_for_template(config_toggles)

    # _extract_error_messages and recent_logs are unused in the agent template;
    # tool sequence already lives in log_summary. Kept in the signature so
    # callers don't have to remember which template needs which arg.
    del recent_logs

    return f"""## 🤖 Auto-Generated by `ha_report_issue` Tool

> This template was auto-generated by the ha_report_issue tool.
> Tool call history was collected automatically to help analyze agent behavior.

**Submit this feedback at:**
{submit_url}

(The submission link above pre-fills the issue title — you don't need to retype it.)

---

## 🤖 What Did the AI Agent Do?

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


---

## 💬 Triggering Prompt & Tool Call

<!-- The AI agent fills this in. Paste, verbatim, the user message that
     prompted the questionable behavior AND the tool call(s) the agent made
     in response. Truncate only after anonymizing tokens / personal names. -->

**User prompt:** <fill in>

**Tool call(s) the agent chose:**
```
<fill in — name + arguments + (truncated) response>
```

---

## 🔧 Tool Calls Made (Auto-Filled)

<details>
<summary>Click to expand tool call sequence</summary>

```
{log_summary}
```

</details>

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

- **ha-mcp Version:** {diagnostic_info.get("ha_mcp_version", "Unknown")}
- **Installation Method:** {diagnostic_info.get("installation_method", "Unknown")}
- **MCP Transport:** {mcp_transport} _(auto-detected — correct if wrong)_
- **MCP Client:** {_format_client_info_for_template(client_info)} _(auto-detected from the MCP `initialize` handshake)_
- **AI Model:**
- **Home Assistant Version:** {diagnostic_info.get("home_assistant_version", "Unknown")}

---

## ⚙️ ha-mcp Configuration

These flags shape which tools the agent sees, so the same behavior may be
expected vs. surprising depending on toggle state:

{config_toggles_section}

---

## 📎 Additional Context

<!-- Screenshots, conversation logs, or other helpful info -->


---

**Note:** This is for improving AI agent behavior. For ha-mcp bugs (errors, crashes), use the Runtime Bug template instead.
"""


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
