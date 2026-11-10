"""Error reporting for config-entry flow steps.

Turns Home Assistant's answers to a flow submit (a 400/422 body, a form
with ``errors``, an ``abort`` step) into structured ``ToolError``s, and holds
the reconfigure outcome vocabulary those errors carry.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from ..redaction import redact_flow_schema, redaction_enabled
from .flow_translations import fetch_flow_strings, flow_text, strings_unread_warning
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowRef:
    """Where a flow's strings live: ``component.<domain>.<path>``.

    ``path`` is ``config`` (config and reconfigure flows), ``options`` or
    ``config_subentries.<type>``. An options or subentry flow can name its
    entry's ``entry_id`` and leave ``domain`` ``None``: the error, abort and
    progress texts then read the domain from the entry, while
    ``describe_form`` shows no help text.
    """

    domain: str | None
    path: str
    entry_id: str | None = None

    def __post_init__(self) -> None:
        if self.path.split(".")[0] not in {"config", "options", "config_subentries"}:
            raise ValueError(f"Not a config entry flow path: {self.path!r}")


async def _flow_texts(
    client: Any,
    flow: FlowRef | None,
    step: dict[str, Any],
    kind: str,
    keys: dict[str, Any],
) -> dict[str, Any]:
    """Core's text for each ``<path>.<kind>.<key>``, as the frontend looks it up.

    ``keys`` maps an output key (a field name, or ``reason_text`` /
    ``progress_action_text``) to its translation key; a key Core ships no
    string for is left out, so the caller keeps the raw key. A failed strings
    read, or a failed read of the entry's domain, gives ``warnings`` instead.
    """
    if flow is None or not keys:
        return {}
    domain, path = flow.domain, flow.path
    if isinstance(step.get("translation_domain"), str):
        # The reason belongs to another integration. A subentry abort reads
        # it under that integration's config strings, as the frontend's dev
        # branch does; the release Core 2026.8.0 pins reads
        # config_subentries.<type>.abort instead.
        domain = step["translation_domain"]
        if kind == "abort" and path.startswith("config_subentries."):
            path = "config"
    elif domain is None and flow.entry_id:
        try:
            domain = (await client.get_config_entry(flow.entry_id)).get("domain")
        except Exception:  # the text is best effort
            logger.warning(
                "domain of %s for flow text failed", flow.entry_id, exc_info=True
            )
            return {
                "warnings": [
                    f"The domain of config entry {flow.entry_id} could not be "
                    "read, so Home Assistant's texts for this flow step are "
                    "missing"
                ]
            }
    if not isinstance(domain, str):
        return {}
    strings = await fetch_flow_strings(client, domain, path.split(".")[0])
    if strings is None:
        return {"warnings": [strings_unread_warning(domain)]}
    placeholders = step.get("description_placeholders")
    texts = {
        name: flow_text(strings, domain, f"{path}.{kind}.{key}", placeholders)
        for name, key in keys.items()
        if isinstance(key, str)
    }
    return {name: text for name, text in texts.items() if text}


class ReconfigureStatus(StrEnum):
    """Outcome vocabulary for the ``ha_set_integration`` reconfigure mode.

    Ordered by how far the request got. Everything from ``APPLIED_*`` onward
    means Home Assistant committed the change, or may have, so a caller must
    not blindly retry those. Every flow also uses ``APPLIED_BUT_UNVERIFIED``
    for a submit or poll whose answer was lost.
    """

    #: The flow was aborted before any value reached Home Assistant.
    FLOW_ABORTED_BEFORE_APPLY = "flow_aborted_before_apply"
    #: Home Assistant rejected the change; nothing was committed.
    APPLY_FAILED = "apply_failed"
    #: Committed, but the flow never consumed every supplied value.
    APPLIED_BUT_INCOMPLETE = "applied_but_incomplete"
    #: Committed, and the entry reloaded cleanly.
    APPLIED_AND_VERIFIED = "applied_and_verified"
    #: Maybe committed: the answer was lost.
    APPLIED_BUT_UNVERIFIED = "applied_but_unverified"


def _parse_flow_api_error(
    api_error: HomeAssistantAPIError,
) -> dict[str, Any]:
    """Extract structured field-level info from an HA flow 4xx response.

    Home Assistant returns voluptuous validation failures during flow
    submission as either:

    - ``{"message": "User input malformed: extra keys not allowed @ data['name']"}``
      (raised before form validation, e.g. unknown field in payload)
    - ``{"errors": {"base": "..."}, "description_placeholders": {...}}``
      (per-field errors after voluptuous validation succeeds)
    - Free-form text (when the body isn't JSON).

    Returns a dict with at least:
      - ``message``: the most informative human-readable string we found.
      - ``field_errors``: dict of field-name -> error code/message, when
        the body contained an ``errors`` map. Empty dict otherwise.
      - ``raw``: the response_data dict (or ``None``) for diagnostics.
    """
    body = api_error.response_data or {}
    field_errors: dict[str, Any] = {}
    message_parts: list[str] = []

    if isinstance(body, dict):
        errors_field = body.get("errors")
        if isinstance(errors_field, dict):
            field_errors = {
                key: val for key, val in errors_field.items() if isinstance(key, str)
            }

        # HA's stock 400 carries a `message` key with the voluptuous detail.
        msg = body.get("message")
        if isinstance(msg, str) and msg.strip():
            message_parts.append(msg.strip())

        # description_placeholders sometimes carry the human-readable error.
        placeholders = body.get("description_placeholders")
        if isinstance(placeholders, dict):
            for key, val in placeholders.items():
                if isinstance(val, str) and val.strip():
                    message_parts.append(f"{key}: {val.strip()}")

    if not message_parts:
        # Fall back to the wrapper exception message ("API error: 400 - ...").
        message_parts.append(str(api_error))

    return {
        "message": " | ".join(dict.fromkeys(message_parts)),  # de-dupe, preserve order
        "field_errors": field_errors,
        "raw": body if isinstance(body, dict) else None,
    }


_FLOW_STILL_OPEN = (
    "The flow is still open: answer the step again with flow_id, giving the "
    "whole answer (nothing from the rejected one is kept), or end it with "
    "flow_id and abort=True."
)


def _build_flow_error_context(
    flow_id: str,
    status_code: int,
    domain: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any],
    submitted: dict[str, Any],
    parsed_raw: dict[str, Any] | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "flow_id": flow_id,
        "status_code": status_code,
        "step_id": current_step.get("step_id"),
        "submitted_keys": sorted(submitted),
    }
    if domain:
        context["domain"] = domain
    if menu_choice:
        context["menu_choice"] = menu_choice
    if parsed_raw is not None:
        context["response_body"] = parsed_raw
    return context


async def _raise_flow_api_error(
    api_error: HomeAssistantAPIError,
    *,
    flow_id: str,
    domain: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any],
    submitted: dict[str, Any],
    is_reconfigure: bool = False,
) -> NoReturn:
    """Translate an HA 4xx during a flow submit into a structured ToolError.

    For 400/422 responses, parses ``response_data`` for field-level info
    via ``_parse_flow_api_error``. The rejected step's own ``data_schema``
    rides along, next to ``field_errors`` or in place of them, so the caller
    sees what Home Assistant accepts. Home Assistant keeps the flow open after
    a rejected submit, and the error says so.

    Always raises ``ToolError`` — never returns.
    """
    parsed = _parse_flow_api_error(api_error)
    field_errors = parsed["field_errors"]
    status_code = api_error.status_code or 0
    subject = f"{domain} flow" if domain else "flow"

    context = _build_flow_error_context(
        flow_id,
        status_code,
        domain,
        menu_choice,
        current_step,
        submitted,
        parsed["raw"],
    )

    suggestions: list[str] = []
    schema = current_step.get("data_schema")
    if isinstance(schema, list):
        # An options/reconfigure step's schema carries the persisted values in
        # description.suggested_value — under redact_secrets those must not
        # ride into the error context verbatim (#2157). Deep-copies, so the
        # live step is untouched.
        context["data_schema"] = (
            redact_flow_schema(schema) if redaction_enabled() else schema
        )
    if field_errors:
        # HA names fields only when the caller's input failed its schema.
        code = ErrorCode.VALIDATION_INVALID_PARAMETER
        context["field_errors"] = field_errors
        readable = ", ".join(f"{k}: {v}" for k, v in field_errors.items())
        message = f"Home Assistant rejected the {subject} step: {readable}"
        suggestions.append(
            "Fix the field(s) listed in 'field_errors' and answer the step again."
        )
    else:
        code = ErrorCode.SERVICE_CALL_FAILED
        message = (
            f"Home Assistant rejected the {subject} step "
            f"({status_code}): {parsed['message']}"
        )
        if "data_schema" in context:
            suggestions.append(
                "Inspect 'data_schema' in this error to see the fields HA expects."
            )

    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED
    suggestions.append(_FLOW_STILL_OPEN)

    raise_tool_error(
        create_error_response(
            code,
            message,
            suggestions=suggestions,
            context=context,
        )
    )


def _flow_failure_context(
    flow_id: str, current_step: dict[str, Any], *, is_reconfigure: bool
) -> dict[str, Any]:
    context: dict[str, Any] = {"flow_id": flow_id, "details": current_step}
    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED
    return context


def redacted_step(step: dict[str, Any]) -> dict[str, Any]:
    """``step`` with its form's stored secrets redacted under redact_secrets.

    A form carries the persisted values in ``suggested_value`` (#2157).
    """
    schema = step.get("data_schema")
    if isinstance(schema, list) and redaction_enabled():
        return {**step, "data_schema": redact_flow_schema(schema)}
    return step


async def raise_form_errors(
    client: Any,
    flow: FlowRef | None,
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool = False,
) -> NoReturn:
    """Raise for a form HA returned with ``errors``, with Core's text for each
    and the form's ``fields`` in the describe shape. The flow stays open."""
    # describe imports config_entry_flow, which imports this module.
    from .config_helpers.describe import compact_schema

    errors = current_step["errors"]
    context: dict[str, Any] = {
        "flow_id": flow_id,
        "step_id": current_step.get("step_id"),
        "errors": errors,
        "fields": compact_schema(current_step.get("data_schema") or []),
    }
    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED
    if isinstance(errors, dict):
        texts = await _flow_texts(client, flow, current_step, "error", errors)
        if warnings := texts.pop("warnings", None):
            context["warnings"] = warnings
        if texts:
            context["error_text"] = texts
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "Form validation failed",
            suggestions=[
                "Fix the field errors and answer the step again with corrected values",
                _FLOW_STILL_OPEN,
            ],
            context=context,
        )
    )


async def abort_reason_text(
    client: Any, flow: FlowRef | None, current_step: dict[str, Any]
) -> dict[str, Any]:
    """``{"reason_text": ...}`` for an abort step, or ``{}`` without a string."""
    return await _flow_texts(
        client, flow, current_step, "abort", {"reason_text": current_step.get("reason")}
    )


async def progress_action_text(
    client: Any, flow: FlowRef | None, current_step: dict[str, Any]
) -> dict[str, Any]:
    """``{"progress_action_text": ...}`` for a progress step, or ``{}``."""
    return await _flow_texts(
        client,
        flow,
        current_step,
        "progress",
        {"progress_action_text": current_step.get("progress_action")},
    )


async def raise_flow_abort(
    client: Any,
    flow: FlowRef | None,
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool = False,
) -> NoReturn:
    """Raise the structured error for an ABORT flow step."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Flow aborted: {current_step.get('reason')}",
            context={
                **_flow_failure_context(
                    flow_id, current_step, is_reconfigure=is_reconfigure
                ),
                **await abort_reason_text(client, flow, current_step),
            },
        )
    )
