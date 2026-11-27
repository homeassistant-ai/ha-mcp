"""One Home Assistant flow step per tool call.

Adding an integration or a helper, changing an entry's options, reconfiguring
an entry and setting a config subentry each run a Home Assistant flow. A call
answers the step Home Assistant shows and returns the next one, with the
``flow_id`` that continues it, as the HA UI does. A caller that already knows
the forms may hand over their answers in order; each goes to the step the
flow is then at, unchanged, and the call stops at the first one Core refuses.
A flow stays open between calls until Home Assistant finishes or aborts it,
or the caller aborts it.
``ha_set_integration``, ``ha_config_set_helper`` and ``ha_manage_updates``
(a Repairs issue's fix flow) drive their flows through here.

Which flow manager a ``flow_id`` belongs to follows from the anchors:
``domain`` is the setup flow, ``entry_id`` the options flow, ``entry_id``
with ``reconfigure=True`` the entry's reconfigure flow (a config flow with the
reconfigure source), whose identity anchors are checked by
``check_reconfigure_identity`` before every call that may submit,
``entry_id`` with a subentry type the subentry flow, and a Repairs issue's
domain and issue_id its fix flow. ``entry_id`` with the ``flow_id`` of the
reauth flow Core opened for that entry (named by its Repairs issue) is that
reauth flow.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Literal, NoReturn, cast

from ..client.repair_flows import abort_repair_flow, submit_repair_flow_step
from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .component_api import COMPONENT_INSTALL_HINT
from .component_flow import flow_record
from .config_entry_flow import (
    _reject_redaction_sentinels,
    config_entry_of_domain,
    refuse_component_entry,
)
from .config_entry_flow_errors import (
    FlowRef,
    ReconfigureStatus,
    _flow_texts,
    raise_flow_abort,
    raise_form_errors,
    step_texts,
)
from .config_entry_flow_form import form_submission
from .config_entry_flow_introspect import menu_choices, shielded_abort
from .config_entry_flow_progress import PROGRESS_STEP_TYPES, flow_url
from .config_entry_flow_submit import (
    EXTERNAL_STEP_TYPES,
    RECONFIGURE_SUCCESS_REASONS,
    _rejected_on_submit,
    _submit_step,
    _wait_for_progress,
    raise_reconfigure_created_entry,
)
from .config_entry_identity import check_reconfigure_identity
from .config_entry_reload_watch import watch_entry_reload
from .config_helpers.describe import describe_form
from .helpers import raise_tool_error, validate_identifier_not_empty
from .ignored_entries import refuse_ignored_entry

# Flows known to belong to a target, flow_id -> FlowTarget.anchor: a flow_id
# continues or ends only with the kind and anchor it was opened with. Home
# Assistant reports neither a user flow's nor a reconfigure flow's source over
# REST or WebSocket, so a flow this server opened is recorded here. An ha-mcp
# restart empties it; a flow_id it does not hold is looked up in Core's own
# flow manager through the component (``component_flow.flow_record``).
opened_flows: dict[str, tuple[str, str | None]] = {}


def reset_opened_flows() -> None:
    """Forget every flow this server opened, as an ha-mcp restart does."""
    opened_flows.clear()


FlowKind = Literal["setup", "options", "reconfigure", "reauth", "subentry", "repair"]

# FlowTarget.kind -> the flow manager holding its flows.
_FLOW_MANAGERS = {
    "setup": "config",
    "reconfigure": "config",
    "reauth": "config",
    "options": "options",
    "subentry": "subentries",
    "repair": "repairs",
}


@dataclass(frozen=True)
class FlowTarget:
    """The flow a call drives; ``domain`` names the integration whose texts
    apply. A subentry flow reconfigures when it names a ``subentry_id`` and
    creates one otherwise. A repair flow fixes ``issue_id``; its texts live
    under ``strings_key`` (the issue's translation_key, else its issue_id)
    and are filled with the issue's ``placeholders``."""

    kind: FlowKind
    domain: str | None
    entry_id: str | None = None
    subentry_type: str | None = None
    subentry_id: str | None = None
    title: str | None = None
    issue_id: str | None = None
    strings_key: str | None = None
    placeholders: Mapping[str, Any] | None = field(default=None, compare=False)

    @property
    def ref(self) -> FlowRef:
        if self.kind == "repair":
            key = self.strings_key or self.issue_id
            return FlowRef(
                self.domain, f"issues.{key}.fix_flow", None, self.placeholders
            )
        if self.kind == "subentry":
            path = f"config_subentries.{self.subentry_type}"
        else:
            path = "options" if self.kind == "options" else "config"
        return FlowRef(self.domain, path, self.entry_id)

    @property
    def anchor(self) -> tuple[str, str | None]:
        if self.kind == "subentry":
            return (
                self.kind,
                f"{self.entry_id}/{self.subentry_type}/{self.subentry_id or ''}",
            )
        if self.kind == "repair":
            return (self.kind, f"{self.domain}/{self.issue_id}")
        return (self.kind, self.entry_id or self.domain)

    @property
    def edits_existing(self) -> bool:
        """Options, reconfigure, reauth, subentry-reconfigure and repair steps
        are posted with the values they show, as the HA dialog posts them."""
        return self.kind != "setup" and (
            self.kind != "subentry" or self.subentry_id is not None
        )

    @property
    def reconfigures(self) -> bool:
        """Whether the flow commits through Core's reconfigure success abort
        (``reauth_successful`` for a reauth flow)."""
        return self.kind in ("reconfigure", "reauth") or self.subentry_id is not None

    def submit_fn(self, client: Any) -> Any:
        if self.kind == "options":
            return client.submit_options_flow_step
        if self.kind == "subentry":
            return client.submit_config_subentry_flow_step
        if self.kind == "repair":
            return partial(submit_repair_flow_step, client)
        return client.submit_config_flow_step

    def abort_fn(self, client: Any) -> Any:
        if self.kind == "options":
            return client.abort_options_flow
        if self.kind == "subentry":
            return client.abort_config_subentry_flow
        if self.kind == "repair":
            return partial(abort_repair_flow, client)
        return client.abort_config_flow

    def url(self, flow_id: str) -> str:
        return flow_url(self.ref, flow_id)


def _raise_flow_not_open(target: FlowTarget, flow_id: str) -> NoReturn:
    # An entry_id with a flow_id names the entry's options flow or the reauth
    # flow Home Assistant opened for it; once the flow is gone Core's record no
    # longer says which, and without flow_id the call opens the options flow.
    of_entry = target.kind == "options"
    kind = "options or reauth" if of_entry else target.kind
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No open {kind} flow {flow_id!r}: Home Assistant finished "
            "or aborted it, or restarted",
            suggestions=[
                "A finished flow needs nothing more. Without flow_id this call "
                "opens the entry's options flow."
                if of_entry
                else "Start the flow again without flow_id."
            ],
            context={"flow_id": flow_id, "flow_kind": target.kind},
        )
    )


def _record_anchor(kind: str, record: dict[str, Any]) -> tuple[str, str | None] | None:
    """The ``FlowTarget.anchor`` of the flow Core recorded, read as a flow of
    ``kind``; ``None`` when it is not one (a discovery or reauth flow is not a
    setup flow a caller started)."""
    handler, source = record.get("handler"), record.get("source")
    if kind == "repair":
        return (kind, f"{handler}/{record.get('issue_id')}")
    if kind == "subentry":
        if not (isinstance(handler, list) and len(handler) == 2):
            return None
        return (kind, f"{handler[0]}/{handler[1]}/{record.get('subentry_id') or ''}")
    if kind == "options":
        return (kind, handler)
    # Core's SOURCE_RECONFIGURE and SOURCE_REAUTH are the kinds' names.
    if kind in ("reconfigure", "reauth") and source == kind:
        return (kind, record.get("entry_id"))
    if kind == "setup" and source == "user":
        return (kind, handler)
    return None


async def _require_opened(client: Any, target: FlowTarget, flow_id: str) -> None:
    """Refuse a flow that was not opened for the call's kind and anchor, so a
    flow_id cannot carry one call's checks over to another flow. A flow this
    server does not hold (it restarted, or Home Assistant opened the flow
    itself, as it opens a reauth flow) is the one Core's record says it is."""
    if opened_flows.get(flow_id) == target.anchor:
        return
    record = None
    if flow_id not in opened_flows:
        record = await flow_record(client, _FLOW_MANAGERS[target.kind], flow_id)
        if record is not None:
            if not record.get("found"):
                _raise_flow_not_open(target, flow_id)
            if _record_anchor(target.kind, record) == target.anchor:
                opened_flows[flow_id] = target.anchor
                return
    _raise_not_opened(
        target, flow_id, unreadable=record is None and flow_id not in opened_flows
    )


def _raise_not_opened(
    target: FlowTarget, flow_id: str, *, unreadable: bool
) -> NoReturn:
    named_by = (
        "the repairs item"
        if target.kind == "repair"
        else "domain or helper_type, entry_id or helper_id, reconfigure, "
        "subentry_type and subentry_id"
    )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Flow {flow_id!r} was not opened as the {target.kind} flow of "
            "this call's target: a flow continues or ends only with the arguments "
            f"that named its target when it was opened ({named_by}). Nothing "
            "was sent to the flow",
            suggestions=[
                "Start the flow again without flow_id."
                + (
                    " ha-mcp did not open the flow (Home Assistant opens a "
                    "reauth flow itself) or restarted since, and "
                    "without a current ha_mcp_tools component it cannot read "
                    f"the flow's target from Home Assistant. {COMPONENT_INSTALL_HINT}"
                    if unreadable
                    else ""
                )
                + " A flow left open ends when Home Assistant restarts."
            ],
            context={"flow_id": flow_id, "flow_kind": target.kind},
        )
    )


async def read_step(client: Any, target: FlowTarget, flow_id: str) -> dict[str, Any]:
    """GET the step of a flow opened for the call's anchor. Core answers it
    by running the step again without input, so a lost answer is classified
    like a submit's."""
    await _require_opened(client, target, flow_id)
    try:
        step = await _submit_step(
            lambda _flow_id, _payload: client._request("GET", target.url(flow_id)),
            flow_id,
            {},
            domain=target.domain,
            last_menu_choice=None,
            current_step={},
            is_reconfigure=target.reconfigures,
        )
    except HomeAssistantAPIError as err:
        if err.status_code == 404:
            _raise_flow_not_open(target, flow_id)
        raise
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
    target: FlowTarget,
    step: dict[str, Any],
    config: dict[str, Any],
    flow_id: str,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The submit for one form: the caller's values, over the values an
    options, reconfigure, reauth or repair form shows, as the HA dialog posts
    it (#2254).
    Core's schema checks the rest. ``defaults`` answers only the fields the
    form declares and ``config`` leaves out."""
    if step.get("type") != "form":
        _refuse(
            f"The flow is at a {step.get('type')} step, which takes no config"
            + ("; pass menu_choice" if step.get("type") == "menu" else ""),
            flow_id=flow_id,
            step_id=step.get("step_id"),
            menu_options=menu_choices(step) if step.get("type") == "menu" else None,
        )
    schema = step.get("data_schema")
    if defaults and isinstance(schema, list):
        declared = {f.get("name") for f in schema if isinstance(f, dict)}
        config = {**{k: v for k, v in defaults.items() if k in declared}, **config}
    return form_submission(schema, config, keep_shown=target.edits_existing)


async def submit_step(
    client: Any,
    target: FlowTarget,
    flow_id: str,
    *,
    config: dict[str, Any] | None,
    menu_choice: str | None,
    current: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer ``current``, the step the flow is at, and return Core's next
    answer, raw. Input Core rejects (the form back with ``errors``, or a 400)
    raises; the flow stays open to correct it.
    """
    if config is not None:
        _reject_redaction_sentinels(config)
    if menu_choice is not None:
        payload = _menu_payload(current, menu_choice, flow_id)
    else:
        payload = _form_payload(target, current, config or {}, flow_id, defaults)
    answer = await _submit_step(
        target.submit_fn(client),
        flow_id,
        payload,
        domain=target.domain,
        last_menu_choice=menu_choice,
        current_step=current,
        is_reconfigure=target.reconfigures,
    )
    if _rejected_on_submit(answer, current if menu_choice is None else None):
        await raise_form_errors(
            client,
            target.ref,
            flow_id,
            answer,
            is_reconfigure=target.reconfigures,
        )
    return answer


async def abort_step_flow(
    client: Any, target: FlowTarget, flow_id: str
) -> dict[str, Any]:
    """End the flow."""
    try:
        await target.abort_fn(client)(flow_id)
    except HomeAssistantAPIError as err:
        if err.status_code == 404:
            _raise_flow_not_open(target, flow_id)
        raise
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
    or reauth flow's success abort and a repair flow's abort that hands over to
    ``next_flow``. What is returned is a form, a menu, ``create_entry`` or
    one of those aborts.
    """
    is_reconfigure = target.reconfigures
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
        target.kind != "repair"
        and step_type == "abort"
        and step.get("reason") in RECONFIGURE_SUCCESS_REASONS
    )
    if entry_updated and not is_reconfigure:
        # A setup flow ended by updating an existing entry
        # (async_update_reload_and_abort answers reauth_successful outside a
        # reconfigure): Core changed an entry no check of this call covered.
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
    handed_over = target.kind == "repair" and bool(step.get("next_flow"))
    if step_type == "abort" and not entry_updated and not handed_over:
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
    """A form or menu step as the call's response, with Core's texts for it
    as the frontend shows them; the flow stays open."""
    texts = await step_texts(client, target.ref, step)
    warnings: list[str] = texts.pop("warnings", [])
    body: dict[str, Any] = {
        "flow_type": step.get("type"),
        "step_id": step.get("step_id"),
        **texts,
    }
    if step.get("type") == "menu":
        body["menu_options"] = menu_choices(step)
        how = "menu_choice"
    else:
        body.update(
            await describe_form(
                client, target.domain, target.ref.path, step, target.placeholders
            )
        )
        warnings += body.pop("warnings", [])
        how = "config"
        errors = step.get("errors")
        if errors:
            body["errors"] = errors
            if isinstance(errors, dict):
                texts = await _flow_texts(client, target.ref, step, "error", errors)
                warnings += texts.pop("warnings", [])
                if texts:
                    body["error_text"] = texts
    if warnings:
        body["warnings"] = list(dict.fromkeys(warnings))
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
    if not isinstance(flow_id, str) or not flow_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Home Assistant started the {target.kind} flow without a flow_id",
                context={"details": step},
            )
        )
    return flow_id


_FIXED = (
    "Home Assistant fixed the issue and removed it from Repairs; an "
    "integration that still finds the problem raises it again."
)


async def _repair_finished(
    client: Any, target: FlowTarget, step: dict[str, Any]
) -> dict[str, Any]:
    """A fix flow's ``create_entry``, or its hand-over to ``next_flow``. Core
    deleted the issue unless the flow aborted."""
    fixed = step.get("type") == "create_entry"
    response: dict[str, Any] = {
        "success": True,
        "domain": target.domain,
        "issue_id": target.issue_id,
        "fixed": fixed,
    }
    if fixed:
        response["message"] = _FIXED
        # The frontend fills the finish text with the step's placeholders only.
        texts = await _flow_texts(
            client,
            replace(target.ref, placeholders=None),
            step,
            "create_entry",
            {"create_entry_text": step.get("description") or "default"},
        )
        response.update(texts)
    if next_flow := step.get("next_flow"):
        response["next_flow"] = next_flow
        response["result"] = step.get("result")
        response["message"] = (
            (
                "Home Assistant fixed the issue and"
                if fixed
                else "The fix flow ended and"
            )
            + " handed over to another flow, next_flow ([flow type, flow_id]); "
            "result is the config entry that flow belongs to (none for a new "
            "entry's setup flow). " + _CONTINUE_NEXT_FLOW
        )
    return response


# How a caller continues the flow Core names in a result's next_flow.
_CONTINUE_NEXT_FLOW = (
    "Continue it with that flow_id, by flow type: config_flow with "
    "ha_set_integration and the integration's domain (a new entry), or that "
    "entry's entry_id (its reauth flow; reconfigure=True and an expected_* "
    "anchor for its reconfigure flow); options_flow with ha_set_integration "
    "and that entry_id; config_subentries_flow with ha_config_set_helper("
    "helper_type='config_subentry'), that entry_id and the subentry type. Or "
    "continue it in Home Assistant's UI."
)


async def _finished_response(
    client: Any, target: FlowTarget, step: dict[str, Any]
) -> dict[str, Any]:
    """``create_entry`` of a setup, options or repair flow, a reconfigure or
    reauth flow's success abort, or a repair flow's hand-over to another flow."""
    if target.kind == "repair":
        return await _repair_finished(client, target, step)
    result = step.get("result")
    result = result if isinstance(result, dict) else {}
    if target.kind in ("reconfigure", "reauth"):
        done = "reconfigured" if target.kind == "reconfigure" else "re-authenticated"
        return {
            "success": True,
            "entry_id": target.entry_id,
            "domain": target.domain,
            "reason": step.get("reason"),
            "message": f"{target.domain} integration {done}",
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
    created: dict[str, Any] = {
        "success": True,
        "entry_id": result.get("entry_id"),
        "title": result.get("title", step.get("title")),
        "domain": target.domain,
        "message": f"{target.domain} integration created successfully",
    }
    if next_flow := step.get("next_flow"):
        # Core opened a flow the setup dialog goes on to (scrape, ntfy and smtp
        # open the new entry's subentry flow, satel_integra its options flow).
        created["next_flow"] = next_flow
        created["message"] += (
            ". Home Assistant opened another flow for the new entry, next_flow "
            "([flow type, flow_id]). " + _CONTINUE_NEXT_FLOW
        )
    return created


async def abort_open_flow(
    client: Any, target: FlowTarget, flow_id: str | None
) -> dict[str, Any]:
    """End a flow this server opened for the call's anchor."""
    if flow_id is None:
        _refuse("abort=True needs the flow_id to end", flow_id=None)
    await _require_opened(client, target, flow_id)
    aborted = await abort_step_flow(client, target, flow_id)
    opened_flows.pop(flow_id, None)
    return aborted


def unsent_answers_warning(unsent: int) -> str:
    return (
        f"The flow finished before the last {unsent} of the answers in config "
        "were sent: Home Assistant asked for fewer forms than config answers. "
        "Check the result."
    )


async def advance_flow(
    client: Any,
    target: FlowTarget,
    *,
    flow_id: str | None,
    config: dict[str, Any] | list[dict[str, Any]] | None,
    menu_choice: str | None,
    start: Any,
    defaults: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Answer steps of a flow; returns its ``flow_id``, Core's last answer
    and how many of ``config``'s answers were not sent.

    ``config`` answers the form the flow is at; a list answers that form and
    the ones after it, one object each, in order. Every answer goes to Core
    as the answer to the step the flow is then at, so an answer Core refuses
    raises with the flow open at that step and the ones after it unsent.
    Without ``flow_id`` the flow is started with ``start()`` and
    ``menu_choice`` answers its opening menu before ``config`` the forms, so
    a flow finishes in this call when ``config`` answers all of them. With
    ``flow_id`` and neither, the step is read again. The answer is a form or
    menu (the flow is open) or the step that ended it; answers are left over
    only when the flow ended before they were used up.
    ``defaults`` goes to the forms ``config`` answers (see ``_form_payload``).
    """
    answers = [config] if isinstance(config, dict) else list(config or [])
    for given in answers:
        # Before a flow is opened for an answer that cannot be sent.
        _reject_redaction_sentinels(given)
    if flow_id is None:
        step = await start()
        flow_id = _require_flow_id(step, target)
        opened_flows[flow_id] = target.anchor
    else:
        if menu_choice is not None and answers:
            _refuse(
                "Pass menu_choice or config, not both: with flow_id each "
                "answers the step the flow is at",
                flow_id=flow_id,
            )
        step = await read_step(client, target, flow_id)
    step = await settle(client, target, flow_id, step)
    pending: list[tuple[str | None, dict[str, Any] | None]] = [
        (None, given) for given in answers
    ]
    if menu_choice is not None:
        pending.insert(0, (menu_choice, None))
    unsent = 0
    for index, (choice, values) in enumerate(pending):
        if step.get("type") not in ("form", "menu"):
            unsent = len(pending) - index
            break
        answer = await submit_step(
            client,
            target,
            flow_id,
            config=values,
            menu_choice=choice,
            current=step,
            defaults=defaults,
        )
        step = await settle(client, target, flow_id, answer)
    if step.get("type") not in ("form", "menu"):
        opened_flows.pop(flow_id, None)
    return flow_id, step, unsent


async def drive_flow(
    client: Any,
    target: FlowTarget,
    *,
    flow_id: str | None,
    config: dict[str, Any] | list[dict[str, Any]] | None,
    menu_choice: str | None,
    abort: bool,
    start: Any,
) -> dict[str, Any]:
    """One call on a setup, options, reconfigure, reauth, subentry or repair
    flow."""
    if abort:
        return await abort_open_flow(client, target, flow_id)
    flow_id, step, unsent = await advance_flow(
        client,
        target,
        flow_id=flow_id,
        config=config,
        menu_choice=menu_choice,
        start=start,
    )
    if step.get("type") in ("form", "menu"):
        return await open_step_answer(client, target, flow_id, step)
    response = await _finished_response(client, target, step)
    if unsent:
        response.setdefault("warnings", []).append(unsent_answers_warning(unsent))
    return response


async def start_setup_flow(client: Any, domain: str) -> dict[str, Any]:
    try:
        step = await client.start_config_flow(domain)
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
    return cast(dict[str, Any], step)


async def options_target(
    client: Any, entry_id: str, expected_domain: str | None = None
) -> FlowTarget:
    """The entry's options flow, refused for entries this tool must not edit
    and, with ``expected_domain``, for an entry of another domain."""
    entry = await config_entry_of_domain(client, entry_id, expected_domain)
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
    title = entry.get("title")
    return FlowTarget(
        "options",
        domain if isinstance(domain, str) else None,
        entry_id,
        title=title if isinstance(title, str) else None,
    )


async def _reauth_target(client: Any, entry_id: str, flow_id: str) -> FlowTarget | None:
    """The reauth flow ``flow_id`` of entry ``entry_id``; ``None`` when
    ``flow_id`` is no reauth flow. Core opens a reauth flow itself and names
    it in the entry's Repairs issue, so only Core's record says what it is."""
    held = opened_flows.get(flow_id)
    if held is not None and held[0] != "reauth":
        return None
    record = await flow_record(client, "config", flow_id)
    if record is None and held is None:
        # Refused as the options flow would be, but before the entry checks
        # that flow makes, which a reauth flow's entry need not pass.
        options = FlowTarget("options", None, entry_id)
        _raise_not_opened(options, flow_id, unreadable=True)
    if not record or not record.get("found") or record.get("source") != "reauth":
        return None
    if record.get("entry_id") != entry_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Flow {flow_id!r} is the reauth flow of config entry "
                f"{record.get('entry_id')!r}, not of {entry_id!r}. Nothing was "
                "sent to the flow",
                suggestions=["Continue it with that entry's entry_id."],
                context={"flow_id": flow_id, "flow_kind": "reauth"},
            )
        )
    handler = record.get("handler")
    refuse_component_entry(handler, action="re-authenticate", entry_id=entry_id)
    target = FlowTarget(
        "reauth", handler if isinstance(handler, str) else None, entry_id
    )
    opened_flows[flow_id] = target.anchor
    return target


async def _entry_target(client: Any, entry_id: str, flow_id: str | None) -> FlowTarget:
    """The entry's options flow, or the reauth flow ``flow_id`` names."""
    if flow_id is not None and (
        reauth := await _reauth_target(client, entry_id, flow_id)
    ):
        return reauth
    return await options_target(client, entry_id)


async def run_integration_step(
    client: Any,
    *,
    domain: str | None,
    entry_id: str | None,
    flow_id: str | None,
    config: dict[str, Any] | list[dict[str, Any]] | None,
    menu_choice: str | None,
    abort: bool,
    expected_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``ha_set_integration`` on a setup (``domain``), options (``entry_id``),
    reauth (``entry_id`` with the reauth flow's ``flow_id``) or reconfigure
    (``entry_id`` with ``expected_identity``) flow.
    A new flow without ``config`` or ``menu_choice`` returns its first step,
    and every reconfigure call except an abort checks the identity anchors
    first."""
    warnings: list[str] = []
    watch: Any = contextlib.nullcontext()
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
            return await start_setup_flow(client, domain)

    else:
        entry_id = validate_identifier_not_empty(entry_id, "entry_id")
        target = await _entry_target(client, entry_id, flow_id)

        async def start() -> dict[str, Any]:
            return cast(dict[str, Any], await client.start_options_flow(entry_id))

    if (
        target.kind in ("reconfigure", "reauth")
        and not abort
        and (flow_id is not None or config is not None or menu_choice is not None)
    ):
        # Subscribed before the submit: Core queues the reload and returns,
        # so only a stream opened first sees it settle.
        watch = watch_entry_reload(client, str(target.entry_id))
    async with watch as settled:
        response = await drive_flow(
            client,
            target,
            flow_id=flow_id,
            config=config,
            menu_choice=menu_choice,
            abort=abort,
            start=start,
        )
        if settled is not None and "reason" in response:  # the flow finished
            reloaded = await settled()
            state = reloaded.get("state") if reloaded else None
            response["status"] = (
                ReconfigureStatus.APPLIED_AND_VERIFIED
                if state == "loaded"
                else ReconfigureStatus.APPLIED_BUT_UNVERIFIED
            )
            response["entry_state"] = state
    if warnings:
        response.setdefault("warnings", []).extend(warnings)
    return response
