"""Repairs issues for ``ha_manage_updates``: ignore, un-ignore and fix."""

from typing import Any, NoReturn

from ..client.repair_flows import start_repair_flow
from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .config_entry_flow import COMPONENT_DOMAIN, is_component_domain
from .config_entry_flow_errors import _parse_flow_api_error
from .helpers import core_error_message, raise_tool_error, ws_failure_code
from .integration_flow_steps import FlowTarget, drive_flow

# Core's code for an issue its registry does not hold.
_UNKNOWN_ISSUE = "unknown_issue"


def _repair_items(
    action: str, repairs: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """``repairs`` as given, refused unless every item names a domain and an
    issue_id, and unless it holds exactly one for ``fix_repair``."""
    items = repairs or []
    if (
        not items
        or (action == "fix_repair" and len(items) != 1)
        or not all(
            isinstance(r, dict)
            and isinstance(r.get("domain"), str)
            and isinstance(r.get("issue_id"), str)
            for r in items
        )
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{action}' requires repairs: a list of "
                "{'domain': ..., 'issue_id': ...} objects"
                + (", exactly one." if action == "fix_repair" else "."),
                suggestions=[
                    "Read domain and issue_id from ha_get_overview's repairs "
                    "(include_dismissed_repairs=True lists ignored ones too)",
                ],
                context={"action": action},
            )
        )
    return items


async def set_repairs_ignored(
    client: Any, action: str, repairs: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Ignore or un-ignore Repairs issues, collecting per-item results; Home
    Assistant decides whether it holds each issue. A transport failure
    propagates and ends the batch."""
    items = _repair_items(action, repairs)
    ignore = action == "ignore_repair"
    results: list[dict[str, Any]] = []
    for item in items:
        context = {"domain": item["domain"], "issue_id": item["issue_id"]}
        reply = await client.send_websocket_message(
            {"type": "repairs/ignore_issue", **context, "ignore": ignore},
            expected_error_codes=frozenset({_UNKNOWN_ISSUE}),
        )
        if reply.get("success"):
            results.append({"success": True, **context, "ignored": ignore})
            continue
        code = reply.get("error_code")
        results.append(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND
                if code == _UNKNOWN_ISSUE
                else ws_failure_code(reply),
                core_error_message(reply.get("error", "")),
                context={**context, "ha_error_code": code},
                suggestions=["Use ha_get_overview() to list Repairs issues"]
                if code == _UNKNOWN_ISSUE
                else None,
            )
        )
    succeeded = sum(1 for r in results if r["success"])
    failed = len(items) - succeeded
    return {
        "success": failed == 0,
        "action": action,
        "requested": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def _refuse_component_issue(domain: str, issue_id: str) -> None:
    """HA-MCP policy: the component's own fixes restart Home Assistant or
    replace the token this server runs on."""
    if not is_component_domain(domain):
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Cannot run the fix flow of ha-mcp's own '{COMPONENT_DOMAIN}' "
            "issues with this tool: their fixes restart Home Assistant or "
            "replace the token this server runs on.",
            suggestions=["Fix it in Home Assistant under Settings > System > Repairs."],
            context={"domain": domain, "issue_id": issue_id},
        )
    )


async def _issue_strings(
    client: Any, domain: str, issue_id: str
) -> tuple[str, dict[str, Any] | None, list[str]]:
    """Where the issue's fix flow texts live and what fills them, as the
    frontend reads them from the issue Core lists: its translation_key (else
    its issue_id) and translation_placeholders, plus a warning when Core does
    not list it."""
    reply = await client.send_websocket_message({"type": "repairs/list_issues"})
    issues = (reply.get("result") or {}).get("issues") if reply.get("success") else None
    for issue in issues or []:
        if issue.get("domain") == domain and issue.get("issue_id") == issue_id:
            return (
                issue.get("translation_key") or issue_id,
                issue.get("translation_placeholders"),
                [],
            )
    return (
        issue_id,
        None,
        [
            f"Home Assistant's Repairs list does not show {domain}/{issue_id}, "
            "so the fix flow's texts were read under its issue_id, without the "
            "issue's placeholders"
        ],
    )


def _raise_start_refused(
    domain: str, issue_id: str, err: HomeAssistantAPIError
) -> NoReturn:
    # Core answers 400 for an issue it does not hold or that is not fixable,
    # and 404 when no flow was created.
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND
            if err.status_code == 404
            else ErrorCode.SERVICE_CALL_FAILED,
            f"Home Assistant did not start the fix flow of {domain}/{issue_id}: "
            f"{_parse_flow_api_error(err)['message']}",
            suggestions=[
                "Check domain, issue_id and is_fixable in ha_get_overview's "
                "repairs; an issue that is not fixable has no fix flow "
                "(ignore_repair dismisses it)."
            ],
            context={
                "domain": domain,
                "issue_id": issue_id,
                "status_code": err.status_code,
            },
        )
    )


async def fix_repair(
    client: Any,
    repairs: list[dict[str, Any]] | None,
    *,
    flow_id: str | None,
    config: dict[str, Any] | list[dict[str, Any]] | None,
    menu_choice: str | None,
    abort: bool,
) -> dict[str, Any]:
    """One step of the fix flow of the single issue in ``repairs``."""
    [item] = _repair_items("fix_repair", repairs)
    domain, issue_id = item["domain"], item["issue_id"]
    _refuse_component_issue(domain, issue_id)
    warnings: list[str] = []
    target = FlowTarget("repair", domain, issue_id=issue_id)
    if not abort:
        key, placeholders, warnings = await _issue_strings(client, domain, issue_id)
        target = FlowTarget(
            "repair",
            domain,
            issue_id=issue_id,
            strings_key=key,
            placeholders=placeholders,
        )

    async def start() -> dict[str, Any]:
        try:
            return await start_repair_flow(client, domain, issue_id)
        except HomeAssistantAPIError as err:
            if err.status_code not in (400, 404):
                raise
            _raise_start_refused(domain, issue_id, err)

    response = await drive_flow(
        client,
        target,
        flow_id=flow_id,
        config=config,
        menu_choice=menu_choice,
        abort=abort,
        start=start,
    )
    if warnings:
        response.setdefault("warnings", []).extend(warnings)
    return response
