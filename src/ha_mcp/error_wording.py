"""Every test of an error's wording, each under the source that sends the text.

Home Assistant Core, the Supervisor, Python and FastMCP send most of the errors
below with no code, status or exception type that tells the cases apart, so
their wording is the only signal. The rest is this project's own text: the
client's ``Command failed: `` prefix and the usage log. A reworded message makes
its test answer False, and the caller then reports the error as it came. A
case that has a code, status or type is decided on that instead and does not
belong here. ``scripts/error_wording.py`` refuses a wording test in any other
module.
"""

from __future__ import annotations

import re
from typing import Any

# The WebSocket client puts this before Core's message.
COMMAND_FAILED = "Command failed: "


def has_command_failed_prefix(text: str) -> bool:
    return text.lower().startswith(COMMAND_FAILED.lower().rstrip())


# --- Core: helpers/entity_registry.py and helpers/device_registry.py --------
# _validate_area_and_labels (2026.12) raises ValueError "Area '<id>' not found.";
# config/entity_registry/update and config/device_registry/update answer it
# ``invalid_info``, the code of every other ValueError an update raises.


def registry_unknown_area(message: str) -> bool:
    return "Area '" in message and "' not found" in message


# --- Core: blueprint/websocket_api.py --------------------------------------
# ws_substitute_blueprint answers ERR_UNKNOWN_ERROR with str(err) for every
# BlueprintException (MissingInput: "Missing input ...", FailedToLoad: "Failed
# to load blueprint: ...") and for an undefined substitution.


def blueprint_missing_input(message: str) -> bool:
    return "missing input" in message.lower()


def blueprint_not_loaded(message: str) -> bool:
    return "failed to load" in message.lower()


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
    """lovelace/dashboard.py DashboardsCollection._process_create_data: "Url
    path needs to contain a hyphen (-)".
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
    """Unauthorized is a HomeAssistantError, so handle_call_service sends every
    one, the admin gate's included, as ``home_assistant_error`` with its text."""
    return (
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
    """BaseEditConfigView.delete, shared by the id- and key-based views,
    answers 400 for an id it does not have."""
    return (response_data or {}).get("message") == "Resource not found"


# --- Supervisor --------------------------------------------------------------
# JobGroup.acquire raises "Another job is running for job group <name>" for the
# transient per-app collision. The job-level JobConcurrency.REJECT path raises
# a bare "Another job is running" for long operations (OS update, data-disk
# wipe), which must not be retried: the "for job group" tail tells them apart.
_JOB_COLLISION = re.compile(r"Another job is running for job group \S+", re.IGNORECASE)


def supervisor_invalid_options(reason: str, slug: str) -> bool:
    """AppConfigurationInvalidError after Core's bridge dropped its key."""
    return reason.startswith(f"App {slug} has invalid options: ")


def supervisor_unserved_endpoint(reply: dict[str, Any]) -> bool:
    """The plain 404 of the Supervisor's router for a path it does not serve;
    Core's bridge relays the text as ``unknown_error`` and drops the status."""
    text = str(reply.get("error", "")).removeprefix(COMMAND_FAILED)
    return reply.get("error_code") == "unknown_error" and text == "404: Not Found"


def supervisor_job_collision(text: str) -> bool:
    """JobGroupExecutionLimitExceeded carries no error key."""
    return _JOB_COLLISION.fullmatch(text.removeprefix(COMMAND_FAILED)) is not None


# --- Python --------------------------------------------------------------------


def single_target_unpack(error: object) -> bool:
    """ValueError of ``[a] = items``; 3.14 adds ", got N" for a list, tuple or dict."""
    text = str(error)
    return "unpack (expected 1)" in text or "unpack (expected 1," in text


def line_continuation(error: object) -> bool:
    """SyntaxError for a backslash outside a string literal."""
    return "line continuation character" in str(error)


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
