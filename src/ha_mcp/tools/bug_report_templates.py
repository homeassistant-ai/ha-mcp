"""Rendering of ha_report_issue reports: the issue body templates and the
formatting and redaction helpers they share."""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlencode

from .helpers import extract_tool_error_message

# Last line of every generated issue body. A paste that lost its end, for
# example because a chat UI closed the code block early, is visibly short.
REPORT_END_MARKER = "<!-- end of ha_report_issue report -->"

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


def _format_tools_entry_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the File & YAML Tools entry status for the report templates."""
    return diagnostic_info.get("tools_entry_status") or "unknown (probe failed)"


def _format_server_entry_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the in-process server entry status for the report templates."""
    return diagnostic_info.get("server_entry_status") or "unknown (probe failed)"


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


def _agent_environment_sections(
    diagnostic_info: dict[str, Any], text: _ReportText
) -> str:
    """Render the environment and configuration blocks of the agent-side reports."""
    config_toggles = diagnostic_info.get("config_toggles") or {}
    mcp_transport = diagnostic_info.get("mcp_transport", "unknown")
    client_info = diagnostic_info.get("mcp_client_info") or {}
    config_toggles_section = (
        f"{_format_config_toggles_for_template(config_toggles)}\n"
        f"- **tool_policy:** `{diagnostic_info.get('tool_policy', 'not probed')}`"
    )
    return f"""## 📊 Environment

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
"""


def _tool_calls_made_section(log_summary: str, include_logs: bool) -> str:
    """Render the auto-filled tool call sequence, or the left-out note."""
    if not include_logs:
        return _LOGS_LEFT_OUT
    return f"""## 🔧 Tool Calls Made (Auto-Filled)

<details>
<summary>Click to expand tool call sequence</summary>

{_fenced(log_summary)}

</details>
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

    return f"""## 🤖 Auto-Generated by `ha_report_issue` Tool

> This report was generated by the ha_report_issue tool.
> Tool call history was collected automatically to help analyze agent behavior.

---

{_user_comment_section(text)}{description_section}
---

{_tool_call_section(text, "Tool call(s) the agent chose:")}
---

{_tool_calls_made_section(log_summary, include_logs)}
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

{_agent_environment_sections(diagnostic_info, text)}
---

## 📎 Additional Context

<!-- Screenshots, conversation logs, or other helpful info -->


---

**Note:** This is for improving AI agent behavior. For ha-mcp bugs (errors, crashes), file a runtime bug report instead.

{REPORT_END_MARKER}
"""


def _generate_feature_request_template(
    diagnostic_info: dict[str, Any],
    log_summary: str,
    *,
    text: _ReportText = _ReportText(),
    include_logs: bool = True,
    text_cap: int | None = None,
) -> str:
    """Build a feature request as a GitHub issue body.

    The tool calls show what the agent tried, which is the evidence that
    ha-mcp cannot do it yet. ``text``, ``include_logs`` and ``text_cap`` work
    as in the runtime bug template.
    """
    text = _shorten_text(text, text_cap)

    if text.description:
        description_section = f"## 💡 Requested Feature\n\n{text.description}\n"
    else:
        description_section = """## 💡 What Should ha-mcp Do?

<!-- The capability the user is asking for -->


## 🎯 Why Is It Needed?

<!-- The problem it solves, in the user's setup -->


## 🔍 What Did the Agent Try?

<!-- The tools tried and why they could not do it -->
"""

    return f"""## 💡 Auto-Generated by `ha_report_issue` Tool

> This report was generated by the ha_report_issue tool.
> The tool calls below show what the agent tried with the current tools.

---

{_user_comment_section(text)}{description_section}
---

{_tool_call_section(text, "Tool call(s) the agent tried:")}
---

{_tool_calls_made_section(log_summary, include_logs)}
---

{_agent_environment_sections(diagnostic_info, text)}
---

{REPORT_END_MARKER}
"""


NEW_ISSUE_URL = "https://github.com/homeassistant-ai/ha-mcp/issues/new"


# GitHub rejects an /issues/new URL of about 8 KB or more with 414 URI Too
# Long (measured 2026-09-30: 8,167 characters loaded, 8,217 did not). That is
# observed behaviour, not a documented limit, so the budget stays below it.
_ISSUE_URL_MAX_CHARS = 7500


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


def _suggested_title(
    report_type: str,
    diagnostic_info: dict[str, Any],
    recent_logs: list[dict[str, Any]],
) -> str:
    """Return the fallback title; a feature request is not named after an error."""
    if report_type == "feature_request":
        return "Feature request"
    return _generate_bug_title(diagnostic_info, recent_logs)


def _generate_search_keywords(
    diagnostic_info: dict[str, Any],
    recent_logs: list[dict[str, Any]],
    feature_title: str | None = None,
) -> list[str]:
    """
    Generate search keywords for duplicate issue detection.

    Returns a list of keywords to search for similar issues. A feature
    request is searched by its title, not by the session's last error.
    """
    if feature_title:
        return [feature_title]
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
