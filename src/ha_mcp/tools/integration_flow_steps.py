"""One Home Assistant flow step per ``ha_set_integration`` call.

Adding an integration, changing an entry's options and reconfiguring an entry
each run a Home Assistant flow. Instead of walking the whole flow from one
``config``, the tool answers the step Home Assistant shows and returns the
next one, with the ``flow_id`` that continues it, as the HA UI does. A flow
stays open between calls until Home Assistant finishes or aborts it, or the
caller aborts it.

Which flow manager a ``flow_id`` belongs to follows from the anchors:
``domain`` is the setup flow, ``entry_id`` the options flow, and
``entry_id`` with ``reconfigure=True`` the entry's reconfigure flow (a config
flow with the reconfigure source), whose identity anchors are checked by
``check_reconfigure_identity`` before every call that may submit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .config_entry_flow import _reject_redaction_sentinels, refuse_component_entry
from .config_entry_flow_errors import (
    FlowRef,
    ReconfigureStatus,
    _flow_texts,
    raise_flow_abort,
)
from .config_entry_flow_form import (
    _RESERVED_CONFIG_KEYS,
    _handle_form_step,
    _ReuseState,
)
from .config_entry_flow_introspect import menu_choices, shielded_abort
from .config_entry_flow_progress import PROGRESS_STEP_TYPES
from .config_entry_flow_walker import (
    _RECONFIGURE_SUCCESS_REASONS,
    EXTERNAL_STEP_TYPES,
    _submit_step,
    _wait_for_progress,
    raise_reconfigure_created_entry,
)
from .config_entry_identity import check_reconfigure_identity
from .config_helpers.describe import compact_schema, describe_form
from .helpers import raise_tool_error, validate_identifier_not_empty
from .ignored_entries import refuse_ignored_entry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowTarget:
    """The flow a call drives: ``kind`` is ``setup``, ``options`` or
    ``reconfigure``; ``domain`` names the integration whose texts apply."""

    kind: str
    domain: str | None
    entry_id: str | None = None

    @property
    def ref(self) -> FlowRef:
        return FlowRef(
            self.domain,
            "options" if self.kind == "options" else "config",
            self.entry_id,
        )

    @property
    def edits_existing(self) -> bool:
        """Options and reconfigure steps arrive filled with the stored values."""
        return self.kind != "setup"

    def submit_fn(self, client: Any) -> Any:
        if self.kind == "options":
            return client.submit_options_flow_step
        return client.submit_config_flow_step

    def abort_fn(self, client: Any) -> Any:
        if self.kind == "options":
            return client.abort_options_flow
        return client.abort_config_flow

    def url(self, flow_id: str) -> str:
        resource = "options/flow" if self.kind == "options" else "flow"
        return f"/config/config_entries/{resource}/{flow_id}"


def _raise_flow_not_open(target: FlowTarget, flow_id: str) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No open {target.kind} flow {flow_id!r}: Home Assistant finished "
            "or aborted it, it belongs to another flow manager, or Home "
            "Assistant restarted",
            suggestions=[
                "Start the flow again without flow_id. A flow_id from domain "
                "continues only with domain, one from entry_id only with "
                "entry_id (plus reconfigure=True for a reconfigure flow)."
            ],
            context={"flow_id": flow_id, "flow_kind": target.kind},
        )
    )


async def read_step(client: Any, target: FlowTarget, flow_id: str) -> dict[str, Any]:
    """GET the flow's current step. Core answers it by running the step again
    without input, so a lost answer is classified like a submit's.

    The step's ``handler`` must be the call's anchor (the domain of a setup or
    reconfigure flow, the entry_id of an options flow), so a flow_id cannot
    carry one call's checks over to another integration's flow. A reconfigure
    flow of another entry in the same domain passes: Home Assistant reports
    no flow's entry (``config_entries/flow/progress`` leaves reconfigure flows
    out), so nothing here can tell.
    """
    try:
        step = await _submit_step(
            lambda _flow_id, _payload: client._request("GET", target.url(flow_id)),
            flow_id,
            {},
            client=client,
            helper_type=None,
            last_menu_choice=None,
            current_step={},
            is_reconfigure=target.kind == "reconfigure",
            flow_stays_open=True,
        )
    except HomeAssistantAPIError as err:
        if err.status_code == 404:
            _raise_flow_not_open(target, flow_id)
        raise
    handler = step.get("handler")
    expected = target.entry_id if target.kind == "options" else target.domain
    if handler is not None and handler != expected:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Flow {flow_id!r} belongs to {handler!r}, not {expected!r}. "
                "Nothing was submitted",
                suggestions=[
                    "Continue a flow with the domain or entry_id that started it."
                ],
                context={"flow_id": flow_id, "flow_kind": target.kind},
            )
        )
    return step


def _refuse(message: str, *, flow_id: str | None, **context: Any) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message,
            suggestions=[
                "The flow is still open: answer this step with flow_id, or end "
                "it with flow_id and abort=True."
            ]
            if flow_id
            else None,
            context={"flow_id": flow_id, **context},
        )
    )


def _menu_payload(
    step: dict[str, Any], menu_choice: str, flow_id: str
) -> dict[str, Any]:
    choices = menu_choices(step)
    if step.get("type") != "menu":
        _refuse(
            f"menu_choice {menu_choice!r} does not apply: the flow is at a "
            f"{step.get('type')} step, not a menu",
            flow_id=flow_id,
            step_id=step.get("step_id"),
        )
    if menu_choice not in choices:
        _refuse(
            f"menu_choice {menu_choice!r} is not offered by menu step "
            f"{step.get('step_id')!r}",
            flow_id=flow_id,
            menu_options=choices,
        )
    return {"next_step_id": menu_choice}


def _form_payload(
    target: FlowTarget, step: dict[str, Any], config: dict[str, Any], flow_id: str
) -> dict[str, Any]:
    """The submit for one form: the caller's values, plus, on an options or
    reconfigure form, every declared field the caller left out at the value
    the form shows (#2254). A key the form does not declare is refused before
    anything is sent."""
    if step.get("type") != "form":
        _refuse(
            f"The flow is at a {step.get('type')} step, which takes no config"
            + ("; pass menu_choice" if step.get("type") == "menu" else ""),
            flow_id=flow_id,
            step_id=step.get("step_id"),
            menu_options=menu_choices(step) if step.get("type") == "menu" else None,
        )
    remaining = dict(config)
    undeclared: set[str] = set()
    payload = _handle_form_step(
        step,
        remaining,
        undeclared,
        set(),
        _ReuseState(),
        keep_current_values=target.edits_existing,
    )
    undeclared |= set(remaining)
    if undeclared:
        _refuse(
            f"Form {step.get('step_id')!r} does not declare: "
            f"{', '.join(sorted(undeclared))}. Nothing was submitted",
            flow_id=flow_id,
            step_id=step.get("step_id"),
            fields=compact_schema(step.get("data_schema") or []),
            **(
                {"note": "Menus are answered with menu_choice, one step per call."}
                if undeclared & _RESERVED_CONFIG_KEYS
                else {}
            ),
        )
    return payload


async def submit_step(
    client: Any,
    target: FlowTarget,
    flow_id: str,
    *,
    config: dict[str, Any] | None,
    menu_choice: str | None,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer the step the flow is at and return Core's next answer, raw.

    ``current`` is the step when the caller already holds it (just started);
    otherwise it is read back. Core keeps the flow open when it rejects the
    input (a form with ``errors``, or a 400).
    """
    if config is not None:
        _reject_redaction_sentinels(config)
    if current is None:
        current = await read_step(client, target, flow_id)
    if menu_choice is not None:
        if config is not None:
            _refuse(
                "Pass menu_choice or config, not both: each answers one step",
                flow_id=flow_id,
            )
        payload = _menu_payload(current, menu_choice, flow_id)
    else:
        payload = _form_payload(target, current, config or {}, flow_id)
    return await _submit_step(
        target.submit_fn(client),
        flow_id,
        payload,
        client=client,
        helper_type=target.domain,
        last_menu_choice=menu_choice,
        current_step=current,
        is_reconfigure=target.kind == "reconfigure",
        flow_stays_open=True,
    )


async def abort_step_flow(
    client: Any, target: FlowTarget, flow_id: str
) -> dict[str, Any]:
    """End the flow. One Home Assistant no longer has counts as ended."""
    try:
        await target.abort_fn(client)(flow_id)
    except HomeAssistantAPIError as err:
        if err.status_code != 404:
            raise
        return {
            "success": True,
            "flow_id": flow_id,
            "aborted": False,
            "message": "The flow was no longer open.",
        }
    return {
        "success": True,
        "flow_id": flow_id,
        "aborted": True,
        "message": "The flow was aborted; nothing it asked for was saved.",
    }


async def settle(
    client: Any, target: FlowTarget, flow_id: str, step: dict[str, Any]
) -> dict[str, Any]:
    """Core's answer, past anything the caller cannot act on.

    A progress step is waited out (up to 120 s; past that the error carries
    ``flow_id`` to poll). An external (browser) step is refused and its flow
    aborted. An abort raises with Core's reason text, except a reconfigure
    flow's success abort. What is returned is a form, a menu,
    ``create_entry`` or that success abort.
    """
    is_reconfigure = target.kind == "reconfigure"
    if step.get("type") in PROGRESS_STEP_TYPES:
        step = await _wait_for_progress(
            client, target.ref, flow_id, step, is_reconfigure=is_reconfigure
        )
    step_type = step.get("type")
    # A reconfigure flow commits only on its success abort: nothing was applied.
    failed = {"status": ReconfigureStatus.APPLY_FAILED} if is_reconfigure else {}
    if step_type in EXTERNAL_STEP_TYPES:
        await shielded_abort(
            target.abort_fn(client), flow_id, failure="abort of an external step"
        )
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"The flow reached an {step_type!r} step (browser or OAuth "
                "authorization), which an MCP client cannot complete; the flow "
                "was aborted",
                suggestions=[
                    "Complete this flow in the Home Assistant UI "
                    "(Settings > Devices & Services)."
                ],
                context={"flow_id": flow_id, "step_id": step.get("step_id"), **failed},
            )
        )
    if step_type == "create_entry" and is_reconfigure:
        raise_reconfigure_created_entry(flow_id, step)
    entry_updated = (
        step_type == "abort" and step.get("reason") in _RECONFIGURE_SUCCESS_REASONS
    )
    if entry_updated and not is_reconfigure:
        # A reauth or reconfigure flow continued with domain: Core changed an
        # existing entry, which no check of this call covered.
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Home Assistant ended flow {flow_id!r} with "
                f"{step.get('reason')!r}: it updated an existing config entry",
                suggestions=[
                    "Check that entry in Home Assistant. Continue a "
                    "reconfigure flow with entry_id and reconfigure=True, "
                    "which checks the entry's identity first."
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    "details": step,
                },
            )
        )
    if step_type == "abort" and not entry_updated:
        await raise_flow_abort(
            client, target.ref, flow_id, step, is_reconfigure=is_reconfigure
        )
    if step_type not in ("form", "menu", "create_entry", "abort"):
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                f"Unexpected flow result type: {step_type}",
                context={"flow_id": flow_id, "step_id": step.get("step_id"), **failed},
            )
        )
    return step


async def open_step_answer(
    client: Any, target: FlowTarget, flow_id: str, step: dict[str, Any]
) -> dict[str, Any]:
    """A form or menu step as the call's response; the flow stays open."""
    body: dict[str, Any] = {
        "flow_type": step.get("type"),
        "step_id": step.get("step_id"),
    }
    if step.get("type") == "menu":
        body["menu_options"] = menu_choices(step)
        how = "menu_choice"
    else:
        described = await describe_form(client, target.domain, target.ref.path, step)
        described.pop("note", None)  # walker advice; here each call answers one form
        body.update(described)
        how = "config"
        errors = step.get("errors")
        if errors:
            body["errors"] = errors
            if isinstance(errors, dict):
                texts = await _flow_texts(client, target.ref, step, "error", errors)
                if warnings := texts.pop("warnings", None):
                    body.setdefault("warnings", []).extend(warnings)
                if texts:
                    body["error_text"] = texts
    return {
        "success": True,
        "flow_id": flow_id,
        **body,
        "message": (
            f"The flow is open at this step. Answer it with flow_id and {how}, "
            "or end it with flow_id and abort=True."
        ),
    }


def _require_flow_id(step: dict[str, Any], target: FlowTarget) -> str:
    flow_id = step.get("flow_id")
    if isinstance(flow_id, str) and flow_id:
        return flow_id
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Home Assistant started the {target.kind} flow without a flow_id",
            context={"details": step},
        )
    )


def _finished_response(target: FlowTarget, step: dict[str, Any]) -> dict[str, Any]:
    """``create_entry`` of a setup or options flow, in the shape
    ``create_config_entry`` and ``update_config_entry_options`` return, or a
    reconfigure flow's success abort."""
    result = step.get("result")
    result = result if isinstance(result, dict) else {}
    if target.kind == "reconfigure":
        return {
            "success": True,
            "entry_id": target.entry_id,
            "domain": target.domain,
            "reason": step.get("reason"),
            "message": f"{target.domain} integration reconfigured",
        }
    if target.kind == "options":
        return {
            "success": True,
            "entry_id": target.entry_id,
            "title": result.get("title", step.get("title")),
            "domain": target.domain,
            "message": f"{target.domain} integration updated successfully",
            "updated": True,
        }
    return {
        "success": True,
        "entry_id": result.get("entry_id"),
        "title": result.get("title", step.get("title")),
        "domain": target.domain,
        "message": f"{target.domain} integration created successfully",
    }


async def drive_flow(
    client: Any,
    target: FlowTarget,
    *,
    flow_id: str | None,
    config: dict[str, Any] | None,
    menu_choice: str | None,
    abort: bool,
    start: Any,
) -> dict[str, Any]:
    """One ``ha_set_integration`` call on a setup, options or reconfigure flow.

    Without ``flow_id`` the flow is started with ``start()``; ``menu_choice``
    then answers its opening menu and ``config`` the form that follows, so a
    one-form flow (or menu then form) finishes in this call. With ``flow_id``
    the step the flow is at is answered, or, with neither, read again.
    """
    if abort:
        if flow_id is None:
            _refuse("abort=True needs the flow_id to end", flow_id=None)
        return await abort_step_flow(client, target, flow_id)
    if flow_id is None:
        step = await start()
        flow_id = _require_flow_id(step, target)
        step = await settle(client, target, flow_id, step)
        if menu_choice is not None and step.get("type") in ("form", "menu"):
            step = await settle(
                client,
                target,
                flow_id,
                await submit_step(
                    client,
                    target,
                    flow_id,
                    config=None,
                    menu_choice=menu_choice,
                    current=step,
                ),
            )
        if config is not None and step.get("type") in ("form", "menu"):
            step = await settle(
                client,
                target,
                flow_id,
                await submit_step(
                    client,
                    target,
                    flow_id,
                    config=config,
                    menu_choice=None,
                    current=step,
                ),
            )
    elif config is None and menu_choice is None:
        step = await settle(
            client, target, flow_id, await read_step(client, target, flow_id)
        )
    else:
        step = await settle(
            client,
            target,
            flow_id,
            await submit_step(
                client, target, flow_id, config=config, menu_choice=menu_choice
            ),
        )
    if step.get("type") not in ("form", "menu"):
        return _finished_response(target, step)
    return await open_step_answer(client, target, flow_id, step)


async def _start_setup_flow(client: Any, domain: str) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], await client.start_config_flow(domain))
    except HomeAssistantAPIError as err:
        # Core answers 404 "Invalid handler specified" for a domain without a
        # config flow, and 400 when the domain's dependencies failed or its
        # flow has no user step: the domain exists, its flow cannot start.
        if err.status_code == 404:
            code, suggestions = ErrorCode.RESOURCE_NOT_FOUND, None
        elif err.status_code == 400:
            code = ErrorCode.SERVICE_CALL_FAILED
            suggestions = [
                "The integration exists but its setup flow cannot start: "
                "Core's text names the failed dependency or the missing "
                "step. Check the Home Assistant log."
            ]
        else:
            raise
        raise_tool_error(
            create_error_response(
                code,
                f"Home Assistant could not start a config flow for {domain!r} ({err})",
                context={"domain": domain},
                suggestions=suggestions,
            )
        )


async def _options_target(client: Any, entry_id: str) -> FlowTarget:
    """The entry's options flow, refused for entries this tool must not edit."""
    entry = await client.get_config_entry(entry_id)
    domain = entry.get("domain")
    refuse_component_entry(domain, action="change the options of", entry_id=entry_id)
    refuse_ignored_entry(entry, action="change the options of", entry_id=entry_id)
    if entry.get("supports_options") is False:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Config entry '{entry_id}' has no options flow "
                "(supports_options is false)",
                suggestions=[
                    "Use reconfigure=True for its connection settings when "
                    "supports_reconfigure is true."
                ],
                context={"entry_id": entry_id, "domain": domain},
            )
        )
    return FlowTarget("options", domain if isinstance(domain, str) else None, entry_id)


async def run_integration_step(
    client: Any,
    *,
    domain: str | None,
    entry_id: str | None,
    flow_id: str | None,
    config: dict[str, Any] | None,
    menu_choice: str | None,
    abort: bool,
    describe: bool,
    expected_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``ha_set_integration`` on a setup (``domain``), options (``entry_id``)
    or reconfigure (``entry_id`` with ``expected_identity``) flow.
    ``describe`` opens the flow without submitting a form; otherwise a new
    setup or options flow's first form gets ``config`` (``{}`` when omitted).
    A new reconfigure flow without ``config`` returns its first step, and
    every reconfigure call except an abort checks the identity anchors first."""
    if describe and (flow_id is not None or config is not None or abort):
        _refuse(
            "describe=True opens a flow and submits no form: drop config, "
            "flow_id and abort",
            flow_id=flow_id,
        )
    warnings: list[str] = []
    if expected_identity is not None:
        entry_id = validate_identifier_not_empty(entry_id, "entry_id")
        entry: dict[str, Any] = {}
        if not abort:
            entry, warnings = await check_reconfigure_identity(
                client, entry_id, expected_identity
            )
        target = FlowTarget("reconfigure", entry.get("domain"), entry_id)

        async def start() -> dict[str, Any]:
            return cast(
                dict[str, Any],
                await client.start_reconfigure_flow(target.domain, entry_id),
            )

    elif domain is not None:
        refuse_component_entry(domain, action="add an entry for")
        target = FlowTarget("setup", domain)

        async def start() -> dict[str, Any]:
            return await _start_setup_flow(client, domain)

    else:
        entry_id = validate_identifier_not_empty(entry_id, "entry_id")
        target = await _options_target(client, entry_id)

        async def start() -> dict[str, Any]:
            return cast(dict[str, Any], await client.start_options_flow(entry_id))

    if (
        target.kind != "reconfigure"
        and flow_id is None
        and not describe
        and config is None
        and menu_choice is None
    ):
        config = {}
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
