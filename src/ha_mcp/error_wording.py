"""Every test of another program's error wording, each beside its source.

Home Assistant Core, the Supervisor, the Marketplace, Python and FastMCP send
the errors below with no code, status or exception type that tells the cases
apart, so their wording is the only signal. A reworded message makes its test
answer False, and the caller then reports the error as it came. A case that
has a code, status or type is decided on that instead and does not belong
here. ``scripts/error_wording.py`` refuses a wording test in any other module.
"""

from __future__ import annotations

import re
from typing import Any

# The WebSocket client puts this before Core's message.
COMMAND_FAILED = "Command failed: "


def has_command_failed_prefix(text: str) -> bool:
    return text.lower().startswith(COMMAND_FAILED.lower().rstrip())


# --- Core: blueprint/websocket_api.py --------------------------------------
# ws_substitute_blueprint answers ERR_UNKNOWN_ERROR with str(err) for both
# MissingInput ("Missing input ...") and FailedToLoad ("Failed to load
# blueprint: ...").


def blueprint_missing_input(message: str) -> bool:
    return "missing input" in message.lower()


def blueprint_not_loaded(message: str) -> bool:
    lowered = message.lower()
    return "failed to load" in lowered or "not found" in lowered


# --- Core: lovelace/websocket.py and lovelace/dashboard.py -----------------
# _handle_errors answers ``config_not_found`` both for a url_path it does not
# know ("Unknown config specified: <url_path>") and for a dashboard with
# nothing saved (ConfigNotFound, "No config found.").
UNKNOWN_CONFIG_PREFIX = "Unknown config specified"
NO_STORED_CONFIG_MESSAGES = frozenset(
    {"No config found.", f"{COMMAND_FAILED}No config found."}
)


def dashboard_unknown(message: str) -> bool:
    return UNKNOWN_CONFIG_PREFIX in message


def dashboard_has_no_config(message: str | None) -> bool:
    return message in NO_STORED_CONFIG_MESSAGES


def dashboard_save_not_supported(message: str) -> bool:
    """LovelaceConfig.async_save, for a YAML dashboard; sent with code ``error``."""
    return message == "Not supported"


def dashboard_save_in_recovery_mode(message: str) -> bool:
    """LovelaceStorage.async_save; sent with the same code ``error``."""
    return message == "Saving not supported in recovery mode"


def hyphenated_url_path(message: str, url_path: str) -> str | None:
    """lovelace/const.py url_slug: "Url path needs to contain a hyphen (-)".
    The path with hyphens for its underscores, when that is a different path."""
    hyphenated = url_path.replace("_", "-")
    refused = "needs to contain a hyphen" in message
    return hyphenated if refused and hyphenated != url_path else None


# --- Core: automation and script validation (voluptuous text) --------------
_INVALID_KEY = re.compile(r"Invalid (trigger|condition) ['\"]([^'\"]+)['\"] specified")


def variables_not_a_mapping(text: str) -> bool:
    return "'variables'" in text and "expected a mapping" in text.lower()


def unexpected_condition_value(text: str) -> bool:
    return "unexpected value for condition" in text.lower()


def invalid_platform_key(text: str) -> re.Match[str] | None:
    """helpers/trigger.py and helpers/condition.py; group 1 is the kind, 2 the key."""
    return _INVALID_KEY.search(text)


# --- Core: websocket_api/commands.py handle_call_service ---------------------


def service_call_unauthorized(code: str | None, message: str) -> bool:
    """Core refuses the call itself with ``unauthorized``; an Unauthorized the
    service raises is a HomeAssistantError, sent as ``home_assistant_error``
    with the exception's text."""
    return code == "unauthorized" or (
        code == "home_assistant_error"
        and message.removeprefix(COMMAND_FAILED) == "Unauthorized"
    )


# --- Core: websocket_api/commands.py handle_render_template -----------------
RENDER_TIMEOUT_TEXT = "Exceeded maximum execution time"


def render_timed_out(message: str, timeout: float) -> bool:
    """Sent as ``template_error``, the code of every other template failure."""
    return message == f"{RENDER_TIMEOUT_TEXT} of {float(timeout)}s"


# --- Core: config/view.py ---------------------------------------------------


def config_resource_not_found(response_data: dict[str, Any] | None) -> bool:
    """EditKeyBasedConfigView.delete answers 400 for an id it does not have."""
    return (response_data or {}).get("message") == "Resource not found"


# --- Supervisor --------------------------------------------------------------
_JOB_COLLISION = re.compile(r"Another job is running for job group \S+", re.IGNORECASE)


def supervisor_invalid_options(reason: str, slug: str) -> bool:
    """AppConfigurationInvalidError after Core's bridge dropped its key; older
    Supervisors send the missing option without the app prefix."""
    return reason.startswith((f"App {slug} has invalid options: ", "Missing option '"))


def supervisor_job_collision(text: str) -> bool:
    """JobGroupExecutionLimitExceeded carries no error key."""
    return _JOB_COLLISION.fullmatch(text.removeprefix(COMMAND_FAILED)) is not None


# --- ha_mcp_tools component before its file services returned codes ---------


def legacy_file_read_code(message: str, path: str) -> str | None:
    if message.startswith("Path not allowed. Allowed paths: "):
        return "path_not_allowed"
    return {
        f"File does not exist: {path}": "not_found",
        f"Backup does not exist: {path}": "not_found",
        f"Path is not a file: {path}": "not_a_file",
        f"Cannot read binary file: {path}. Only text files are supported.": "binary_file",
    }.get(message)


# --- Python --------------------------------------------------------------------


def single_target_unpack(error: object) -> bool:
    """ValueError of ``[a] = items``; 3.14 adds ", got N" for a list, tuple or dict."""
    text = str(error)
    return "unpack (expected 1)" in text or "unpack (expected 1," in text


def line_continuation(error: object) -> bool:
    """SyntaxError for a backslash outside a string literal."""
    return "line continuation character" in str(error)


def error_mentions(error: Exception, *needles: str) -> bool:
    """Whether the type name or the text of a sandbox error holds a needle."""
    kind, text = type(error).__name__, str(error)
    return any(needle in kind or needle in text for needle in needles)


# --- FastMCP -----------------------------------------------------------------


def unknown_tool(error: Exception, name: str) -> bool:
    """The dispatcher's own NotFoundError, not one raised inside a running tool."""
    return str(error) == f"Unknown tool: {name!r}"


# --- ha-mcp's usage log --------------------------------------------------------


def search_keywords(error_message: str) -> set[str]:
    """Issue-search keywords for a logged tool error."""
    lowered = error_message.lower()
    found = {word for word in ("connection", "timeout", "not found") if word in lowered}
    if "auth" in lowered:
        found.add("authentication")
    return found
