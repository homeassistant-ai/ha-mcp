"""Shared presentation for locally established uncertain service outcomes."""

from typing import Any

from ..client.rest_client import HomeAssistantCommandError
from ..client.service_errors import (
    HomeAssistantServiceAPIOutcomeUnknown,
    HomeAssistantServiceAuthOutcomeUnknown,
    HomeAssistantWebSocketServiceError,
    is_service_outcome_unknown,
)
from ..errors import ErrorCode, create_error_response


def create_service_outcome_unknown_error(
    error: Exception, *, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Retain diagnostics while requiring readback before another service attempt."""
    details = dict(context or {})
    domain, service = details.get("domain"), details.get("service")
    label = f"{domain}.{service}" if domain and service else "the service call"
    details.update(
        write_outcome_unknown=True,
        ha_error_code=(
            error.code
            if isinstance(
                error,
                (
                    HomeAssistantCommandError,
                    HomeAssistantWebSocketServiceError,
                    HomeAssistantServiceAuthOutcomeUnknown,
                ),
            )
            else None
        ),
    )
    if (
        isinstance(
            error,
            (
                HomeAssistantServiceAuthOutcomeUnknown,
                HomeAssistantServiceAPIOutcomeUnknown,
            ),
        )
        and error.status_code is not None
    ):
        details["http_status"] = error.status_code
    response = create_error_response(
        ErrorCode.SERVICE_CALL_FAILED,
        f"The outcome of {label} is unknown; it may have taken effect.",
        details=str(error),
        context=details,
        suggestions=[
            "Check the target state and Home Assistant logs before retrying; "
            "the service may already have acted."
        ],
    )
    return response


def create_service_item_error(
    error: Exception, *, context: dict[str, Any]
) -> dict[str, Any]:
    """Keep typed uncertainty when collecting a failed batch item."""
    if is_service_outcome_unknown(error):
        return create_service_outcome_unknown_error(error, context=context)
    response = create_error_response(
        ErrorCode.SERVICE_CALL_FAILED, str(error), context=context
    )
    return response


def record_service_failure(
    response: dict[str, Any], error: Exception, *, field: str, entity_id: str
) -> None:
    """Preserve a saved configuration while reporting its runtime action separately."""
    item = create_service_item_error(
        error, context={"entity_id": entity_id, "action": field}
    )
    unknown = item.get("write_outcome_unknown") is True
    response[field] = None if unknown else False
    if unknown:
        response["write_outcome_unknown"] = True
        response.setdefault("service_errors", []).append(item)
        warning = (
            f"The requested change was applied. {item['error']['message']} "
            f"{item['error']['suggestion']}"
        )
    else:
        description = {
            "enabled_applied": "Automation config was written, but the requested enabled state could not be applied",
            "actions_triggered": "The requested change was applied, but the actions could not be run",
            "activated": "Scene config was written, but the scene could not be activated",
        }[field]
        warning = f"{description}: {error}"
    response.setdefault("warnings", []).append(warning)


def project_service_reload_results(
    targets: list[tuple[str, tuple[str, str]]], outcomes: list[Any]
) -> dict[str, Any]:
    """Attribute gathered reload failures without hiding uncertain service effects."""
    reloaded, warnings, service_errors = [], [], []
    for (target, _), outcome in zip(targets, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            if is_service_outcome_unknown(outcome):
                item = create_service_item_error(outcome, context={"target": target})
                service_errors.append(item)
                warnings.append(
                    f"{target}: {item['error']['message']} {item['error']['suggestion']}"
                )
            elif "not found" not in str(outcome).lower():
                warnings.append(f"{target}: {outcome}")
        else:
            reloaded.append(target)
    response: dict[str, Any] = {
        "success": True,
        "message": f"Reloaded {len(reloaded)} components",
        "reloaded": reloaded,
    }
    if warnings:
        response["warnings"] = warnings
    if service_errors:
        response.update(write_outcome_unknown=True, service_errors=service_errors)
    return response
