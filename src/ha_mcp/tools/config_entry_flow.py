"""
Config-entry flow machinery shared by the helper, integration and backup tools.

``ha_config_set_helper`` and ``ha_set_integration`` answer one flow step per
call (``integration_flow_steps``), as the HA UI does. What lives here is the
part no caller answers step by step: a backup restore replays a stored
snapshot into a flow's forms (``restore_config_entry_options``,
``recreate_flow_helper`` and, for subentries, ``config_subentry_restore``),
and the guards for ha-mcp's own config entries.

The step machinery lives in sibling modules, each importing only modules
upstream of it:

- ``config_entry_flow_introspect``: the flow-type vocabulary and the shielded
  flow aborts
- ``config_entry_flow_errors``: Home Assistant's flow answers as structured
  errors, and the reconfigure outcome vocabulary
- ``config_entry_flow_form``: one form's submit, as the HA dialog builds it
- ``config_entry_flow_progress``: waiting out a progress step within a budget
- ``config_entry_flow_submit``: submitting a step and classifying its answer
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, NoReturn

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantError,
)
from ..errors import ErrorCode, create_error_response
from ..redaction import placeholder_option_keys
from .component_flow import flow_outline, steps_after
from .config_entry_flow_form import MISSING, shown_value
from .config_entry_flow_introspect import (
    _FlowType,
    abort_failed_flow_type_only,
)
from .config_entry_flow_submit import _rejected_on_submit
from .helpers import raise_tool_error
from .ignored_entries import refuse_ignored_entry

logger = logging.getLogger(__name__)

# A submit left unanswered this long counts as a lost reply.
_SUBMIT_TIMEOUT_S = 20.0

OptionsRestoreReason = Literal[
    "unsupported_form",
    "unsupported_fields",
    "identity_changed",
    "validation_failed",
    "flow_aborted",
]
_RESTORE_REASON_MESSAGES: dict[OptionsRestoreReason, str] = {
    "unsupported_form": "Options restore requires an authoritative options form",
    "unsupported_fields": "Snapshot fields are not accepted by the options form",
    "identity_changed": "Snapshot fields no restore form offers differ from the "
    "stored ones (set at creation); restore refused",
    "validation_failed": "Home Assistant rejected the restored options as invalid",
    "flow_aborted": "Home Assistant aborted the options restore",
}
# These are diagnostic labels, not an allowlist of restorable fields. Arbitrary
# snapshot keys and HA error payloads may contain values and must not be echoed.
_SAFE_RESTORE_FIELD_NAMES = frozenset(
    {
        "additional_options",
        "availability",
        "availability_template",
        "device_class",
        "icon",
        "name",
        "state",
        "state_class",
        "template_type",
        "unit_of_measurement",
    }
)


class OptionsFlowError(HomeAssistantError):
    """Complete-restore failure with submission knowledge for reconciliation."""

    reason_messages: ClassVar[dict[OptionsRestoreReason, str]] = (
        _RESTORE_REASON_MESSAGES
    )

    def __init__(
        self,
        message: str,
        *,
        apply_status: Literal["not_applied", "unknown", "applied"],
        entry_id: str,
        flow_id: str | None = None,
        reason: OptionsRestoreReason | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.fields = tuple(
            sorted(
                field
                for field in set(fields)
                if all(
                    segment in _SAFE_RESTORE_FIELD_NAMES for segment in field.split(".")
                )
            )
        )
        if reason is not None:
            message = self.reason_messages[reason]
            if self.fields:
                message += ": " + ", ".join(self.fields)
        super().__init__(message)
        self.apply_status = apply_status
        self.entry_id = entry_id
        self.flow_id = flow_id


class CreationFlowError(OptionsFlowError):
    """Complete-snapshot creation failure with conservative application knowledge."""

    reason_messages: ClassVar[dict[OptionsRestoreReason, str]] = {
        "unsupported_form": "Helper recreation requires an authoritative creation form",
        "unsupported_fields": "Snapshot fields are not accepted by the creation form",
        "validation_failed": "Home Assistant rejected the recreated helper as invalid",
        "flow_aborted": "Home Assistant aborted the helper recreation",
    }


@dataclass
class _OptionsFlowProgress:
    """Track a restore flow's replies before they are interpreted.

    A complete snapshot may span several forms: each form takes the snapshot
    fields it declares. Before the form HA marks as the last (``last_step``),
    every snapshot field no form took must equal ``fixed``, the entry's stored
    options, since the options flow cannot change it.
    """

    entry_id: str
    flow_id: str | None = None
    apply_status: Literal["not_applied", "unknown", "applied"] = "not_applied"
    reason: OptionsRestoreReason | None = None
    fields: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    fixed: dict[str, Any] | None = None
    current_step: dict[str, Any] = field(default_factory=dict)
    consumed: set[str] = field(default_factory=set)
    error_type: ClassVar[type[OptionsFlowError]] = OptionsFlowError
    operation: ClassVar[str] = "Options restore"

    def failure(self) -> OptionsFlowError:
        messages = {
            "not_applied": f"{self.operation} was refused before application",
            "unknown": f"{self.operation} got no completion reply; the change may have been applied",
            "applied": f"{self.operation} completed but its result could not be processed",
        }
        return self.error_type(
            messages[self.apply_status],
            apply_status=self.apply_status,
            entry_id=self.entry_id,
            flow_id=self.flow_id,
            reason=self.reason,
            fields=self.fields,
        )

    async def submit(
        self,
        client: Any,
        flow_id: str,
        payload: dict[str, Any],
        *,
        submit_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> dict[str, Any]:
        if self.current_step.get("type") == _FlowType.FORM:
            self.check_form(self.current_step)
        self.apply_status = "unknown"
        try:
            submit = submit_fn or client.submit_options_flow_step
            result: dict[str, Any] = await asyncio.wait_for(
                submit(flow_id, payload), timeout=_SUBMIT_TIMEOUT_S
            )
        except HomeAssistantAuthError:
            self.apply_status = "not_applied"
            raise
        except HomeAssistantAPIError as err:
            if err.status_code is not None and 400 <= err.status_code < 500:
                # HA rejected the submission (a malformed or missing field)
                # without applying it; say so rather than refusing nameless.
                self.apply_status = "not_applied"
                self.reason = "validation_failed"
            raise
        self.record_reply(result)
        return result

    def refuse(
        self, reason: OptionsRestoreReason, fields: list[str] | tuple[str, ...] = ()
    ) -> NoReturn:
        self.reason, self.fields = reason, tuple(fields)
        raise self.failure()

    def start(self, step: dict[str, Any]) -> None:
        """Accept the options flow's first reply only as a clean form."""
        if step.get("type") == _FlowType.ABORT:
            # HA refused to open the flow (a second Forecast.Solar plane
            # without an API key aborts as api_key_required).
            self.refuse("flow_aborted")
        if step.get("type") != _FlowType.FORM:
            self.refuse("unsupported_form")
        self._refuse_form_errors(step)
        self.current_step = step

    def _refuse_form_errors(self, step: dict[str, Any]) -> None:
        if errors := step.get("errors"):
            self.refuse(
                "validation_failed", tuple(errors) if isinstance(errors, dict) else ()
            )

    def check_form(self, step: dict[str, Any]) -> None:
        """Before submitting a form, settle the snapshot fields it leaves.

        The leftover check runs only on the form HA marks as last
        (``last_step: True``). A flow that never marks one (``None``, the
        ``async_show_form`` default) is submitted as it comes, and its result
        is verified by readback after it has been applied.
        """
        schema = step.get("data_schema")
        if not isinstance(schema, list):
            self.refuse("unsupported_form")
        self._refuse_form_errors(step)
        # Top-level names: a section's fields sit under the section's key.
        declared = {f.get("name") for f in schema if isinstance(f, dict)}
        mine = {key: value for key, value in self.config.items() if key in declared}
        if unknown := _unknown_snapshot_fields(schema, mine):
            self.refuse("unsupported_fields", unknown)
        self.consumed |= mine.keys()
        if step.get("last_step") is not True:
            # A form follows (False), or may, chosen from these answers (None,
            # as filter's): later forms take the rest, and a final readback
            # verifies what a flow without a marked last form saved.
            return
        left = [key for key in self.config if key not in self.consumed]
        fixed = self.fixed or {}
        if unknown := [key for key in left if key not in fixed]:
            self.refuse("unsupported_fields", unknown)
        if changed := [
            key for key in left if _json_differs(fixed[key], self.config[key])
        ]:
            self.refuse("identity_changed", changed)

    def record_reply(self, result: dict[str, Any]) -> None:
        """Record application knowledge before a reply is interpreted."""
        previous, self.current_step = self.current_step, result
        if result.get("type") == _FlowType.CREATE_ENTRY:
            self.apply_status = "applied"
        elif result.get("type") == _FlowType.ABORT:
            self.apply_status = "not_applied"
            self.reason = "flow_aborted"
        elif result.get("type") == _FlowType.FORM and result.get("errors"):
            # Complete-restore callers require an options form that does not
            # commit when it reports a validation rejection.
            self.apply_status = "not_applied"
            self.reason = "validation_failed"
            if isinstance(result["errors"], dict):
                self.fields = tuple(result["errors"])
        elif (
            result.get("type") == _FlowType.FORM
            and previous.get("last_step") is not True
        ):
            # The next form of the flow: nothing is committed before
            # CREATE_ENTRY; check_form settles it and the replay fills it.
            self.apply_status = "not_applied"
        else:
            # A complete snapshot cannot choose a menu branch mid-flow, nor
            # fill a form after the one HA called the last.
            if result.get("type") in (_FlowType.FORM, _FlowType.MENU):
                self.apply_status = "not_applied"
                self.reason = "unsupported_form"
            raise self.failure()


def _json_differs(stored: Any, snapshot: Any) -> bool:
    """Type-sensitive comparison, as the restore preview shows it (False is not 0)."""
    from ..backup_diff import _compute_json_patch

    patch: list[dict[str, Any]] = []
    _compute_json_patch(snapshot, stored, 1, patch)
    return bool(patch)


def _unknown_snapshot_fields(
    schema: list[Any], config: dict[str, Any], prefix: str = ""
) -> list[str]:
    fields = {
        field["name"]: field
        for field in schema
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }
    unknown: list[str] = []
    for name, value in config.items():
        path = f"{prefix}.{name}" if prefix else name
        if name not in fields:
            unknown.append(path)
        elif isinstance(fields[name].get("schema"), list) and isinstance(value, dict):
            unknown.extend(
                _unknown_snapshot_fields(fields[name]["schema"], value, path)
            )
    return unknown


class _CreationFlowProgress(_OptionsFlowProgress):
    """Fill the creation forms from a complete snapshot; a key no form takes
    is refused before the form HA marks as last."""

    error_type = CreationFlowError
    operation = "Helper recreation"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(entry_id="", config=dict(config))

    def record_reply(self, result: dict[str, Any]) -> None:
        previous_type = self.current_step.get("type")
        if previous_type in (None, _FlowType.MENU) and result.get("type") in (
            _FlowType.MENU,
            _FlowType.FORM,
        ):
            self.current_step = result
            self.apply_status = "not_applied"
            return
        super().record_reply(result)
        if self.apply_status == "applied" and isinstance(result.get("result"), dict):
            entry_id = result["result"].get("entry_id")
            if isinstance(entry_id, str):
                self.entry_id = entry_id
        if self.apply_status == "applied" and previous_type != _FlowType.FORM:
            self.reason = "unsupported_form"
            raise self.failure()

    async def submit(
        self, client: Any, flow_id: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return await super().submit(
            client, flow_id, payload, submit_fn=client.submit_config_flow_step
        )


async def _creation_fields(
    client: Any, helper_type: str
) -> dict[str, set[str] | None] | None:
    """For each step of a creation flow, the fields its form and the forms
    after it declare, read from Core's declaration of the flow. ``None`` for a
    step whose forms Core builds from the answers, and for the whole flow when
    no component reads the declaration.
    """
    steps = await flow_outline(client, helper_type, "config")
    if steps is None:
        return None
    declared: dict[str, set[str] | None] = {}
    for step in steps:
        chain = [
            step,
            *(later for later, _ in steps_after(steps, step["step_id"]) or []),
        ]
        if any(link.get("schema_from_answers") for link in chain):
            declared[step["step_id"]] = None
            continue
        declared[step["step_id"]] = {
            field["name"]
            for link in chain
            for field in link.get("data_schema") or []
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
    return declared


async def _menu_branch_from_snapshot(
    client: Any,
    progress: _OptionsFlowProgress,
    menu: dict[str, Any],
    helper_type: str,
) -> str:
    """The creation menu's branch the snapshot names.

    A menu-rooted helper stores its branch among its options (template's
    ``template_type``, group's ``group_type``), and no creation form takes
    that option. Another option's value can be a menu name too (a template
    binary_sensor's ``device_class`` "light"): then the branch is the one
    whose forms, as Core declares them, take the snapshot's other options.
    Without that declaration such a snapshot is refused.
    """
    options = menu.get("menu_options")
    choices = options if isinstance(options, list) else list(options or {})
    config = progress.config
    keys = [key for key, value in config.items() if value in choices]
    declared = await _creation_fields(client, helper_type) if len(keys) > 1 else None
    if declared is not None:

        def takes_the_rest(key: str) -> bool:
            fields = declared.get(config[key])
            return fields is None or set(config) - {key} <= fields

        keys = [key for key in keys if takes_the_rest(key)]
    if len(keys) != 1:
        progress.refuse("unsupported_form")
    return str(config.pop(keys[0]))


def _snapshot_answers(step: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The snapshot's values for the fields a form declares.

    A field the snapshot does not hold is left out, which is how a form
    clears it; a required one goes back as the form shows it.
    """
    answers: dict[str, Any] = {}
    for declared in step.get("data_schema") or []:
        name = declared.get("name") if isinstance(declared, dict) else None
        if not isinstance(name, str):
            continue
        if name in config:
            answers[name] = config[name]
        elif not declared.get("required"):
            continue
        elif isinstance(declared.get("schema"), list):
            answers[name] = {}
        elif (shown := shown_value(declared)) is not MISSING:
            answers[name] = shown
    return answers


# A restore flow is its forms, plus one creation menu: more replies than this
# is a flow that loops.
_MAX_REPLAY_STEPS = 10


async def replay_snapshot(
    client: Any,
    progress: _OptionsFlowProgress,
    step: dict[str, Any],
    *,
    menu_branch: Callable[[dict[str, Any]], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """Answer a restore flow's forms from ``progress.config``, the snapshot.

    Returns the step that ended the flow. ``progress`` checks each form
    against the snapshot before it is submitted and records what is known
    about the write; a reply it cannot place (a second menu, a progress or
    external step, a form after the last) raises its failure. A form Home
    Assistant returns with errors, and a flow that does not end, raise it too.
    """
    flow_id = progress.flow_id or ""
    for _ in range(_MAX_REPLAY_STEPS):
        step_type = step.get("type")
        if step_type == _FlowType.MENU and menu_branch is not None:
            payload = {"next_step_id": await menu_branch(step)}
            menu_branch = None
        elif step_type == _FlowType.FORM:
            payload = _snapshot_answers(step, progress.config)
        else:
            return step
        answered = step if step_type == _FlowType.FORM else None
        step = await progress.submit(client, flow_id, payload)
        if _rejected_on_submit(step, answered):
            break
    raise progress.failure()


async def recreate_flow_helper(
    client: Any, helper_type: str, options: dict[str, Any]
) -> dict[str, Any]:
    """Create a deleted helper again from its complete options snapshot.

    Every option must be taken by a creation form, or name the creation
    menu's branch. An uncertain or completed creation flow is left open for
    reconciliation.
    """
    progress = _CreationFlowProgress(options)
    try:
        _reject_redaction_sentinels(options)
        progress.apply_status = "unknown"
        initial_step = await client.start_config_flow(helper_type)
        progress.flow_id = initial_step.get("flow_id")
        progress.record_reply(initial_step)
        if not progress.flow_id:
            raise progress.failure()
        result = await replay_snapshot(
            client,
            progress,
            initial_step,
            menu_branch=lambda menu: _menu_branch_from_snapshot(
                client, progress, menu, helper_type
            ),
        )
        entry = result.get("result")
        if result.get("type") != _FlowType.CREATE_ENTRY or not isinstance(entry, dict):
            # Without the created entry the caller cannot identify the helper.
            raise progress.failure()
        return {
            "success": True,
            "entry_id": entry.get("entry_id"),
            "title": entry.get("title"),
            "domain": helper_type,
            "message": f"{helper_type} helper recreated successfully",
        }
    except (Exception, asyncio.CancelledError) as err:
        failure = err if isinstance(err, CreationFlowError) else progress.failure()
        await _cleanup_snapshot_creation(client, progress, failure)
        if isinstance(err, (CreationFlowError, asyncio.CancelledError)):
            raise
        raise failure from err


async def _cleanup_snapshot_creation(
    client: Any, progress: _CreationFlowProgress, failure: OptionsFlowError
) -> None:
    if not progress.flow_id:
        return
    if failure.apply_status == "not_applied":
        await abort_failed_flow_type_only(
            client.abort_config_flow,
            progress.flow_id,
            "Helper recreation flow %s cleanup failed (reason=%s, error_type=%s)",
            progress.flow_id,
            failure.reason,
        )
    else:
        logger.warning(
            "Helper recreation flow %s was not aborted "
            "(apply_status=%s, reason=%s); reconcile Home Assistant before retrying",
            progress.flow_id,
            failure.apply_status,
            failure.reason,
        )


def _reject_redaction_sentinels(config_dict: dict[str, Any]) -> None:
    """Reject config values that are redaction placeholders (#2157).

    A caller round-tripping a redacted read back through a flow write would
    overwrite the live credential with the placeholder string. Omitting the
    key keeps the current value, so rejection loses nothing. Active
    regardless of the redact_secrets toggle: a sentinel captured while
    redaction was on must not overwrite a credential after the operator
    turns it off.
    """
    placeholder_keys = placeholder_option_keys(config_dict)
    if placeholder_keys:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "config contains redaction placeholder values for: "
                f"{', '.join(placeholder_keys)}. These came from a redacted "
                "read, not real values — omit these keys to keep the "
                "current values, or submit the real value.",
                context={"parameter": "config"},
            )
        )


async def _subentry_ids(client: Any, entry_id: str) -> set[str] | None:
    """IDs of the entry's subentries; None when they can't be listed."""
    try:
        listed = await client.list_config_subentries(entry_id)
    except Exception:
        logger.debug("Listing subentries of %s failed", entry_id, exc_info=True)
        return None
    if not isinstance(listed, dict) or not listed.get("success"):
        return None
    return {
        s["subentry_id"]
        for s in listed.get("result") or []
        if isinstance(s, dict) and s.get("subentry_id")
    }


async def _created_subentry_id(
    client: Any, entry_id: str, before: set[str]
) -> str | None:
    created = (await _subentry_ids(client, entry_id) or before) - before
    return created.pop() if len(created) == 1 else None


_SUBENTRY_CREATE_LOCKS: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def subentry_create_lock(entry_id: str) -> asyncio.Lock:
    """The lock a subentry create under ``entry_id`` holds: a create finds its
    subentry by diffing the parent's listing, so creates under one parent run
    one at a time."""
    return _SUBENTRY_CREATE_LOCKS[entry_id]


async def restore_config_entry_options(
    client: Any,
    entry_id: str,
    options: dict[str, Any],
    *,
    expected_domain: str | None = None,
    fixed_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Put a complete options snapshot back through the entry's options flow.

    Each options form takes the snapshot fields it declares; a field the
    snapshot lacks is left out, so a current value absent from it is cleared
    as the form clears it. ``fixed_options`` is the entry's stored options: a
    snapshot field none of the forms offers must still hold its stored value,
    checked before the form HA marks as last (a flow that marks none is
    verified by the caller's readback after it applied). The caller must
    ensure the integration replaces its options only when its flow returns
    CREATE_ENTRY. Raises ``OptionsFlowError`` with apply knowledge on failure;
    an uncertain or completed restore flow is not aborted.

    ``expected_domain`` refuses an entry of another domain (``None`` accepts
    any).
    """
    progress = _OptionsFlowProgress(
        entry_id, config=dict(options), fixed=dict(fixed_options or {})
    )
    try:
        return await _restore_config_entry_options(
            client, entry_id, expected_domain, progress
        )
    except (Exception, asyncio.CancelledError) as err:
        failure = err if isinstance(err, OptionsFlowError) else progress.failure()
        if failure.flow_id and failure.apply_status != "not_applied":
            logger.warning(
                "Options restore flow %s for entry %s was not aborted after failure "
                "(apply_status=%s, reason=%s, fields=%s, error_type=%s); "
                "reconcile Home Assistant state before retrying",
                failure.flow_id,
                failure.entry_id,
                failure.apply_status,
                failure.reason,
                failure.fields,
                type(err).__name__,
            )
        if isinstance(err, (OptionsFlowError, asyncio.CancelledError)):
            raise
        raise failure from err


async def config_entry_of_domain(
    client: Any, entry_id: str, expected_domain: str | None
) -> dict[str, Any]:
    """The config entry, refused when it belongs to another domain."""
    config_entry: dict[str, Any] = await client.get_config_entry(entry_id)
    actual_domain = config_entry.get("domain")
    if expected_domain is not None and actual_domain != expected_domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entry_id '{entry_id}' belongs to domain '{actual_domain}', not '{expected_domain}'",
                suggestions=[
                    f"Use ha_get_integration(domain='{expected_domain}') to find valid entry IDs",
                ],
                context={
                    "entry_id": entry_id,
                    "expected": expected_domain,
                    "actual": actual_domain,
                },
            )
        )
    return config_entry


COMPONENT_DOMAIN = "ha_mcp_tools"

COMPONENT_ENTRY_SUGGESTIONS: tuple[str, ...] = (
    "Manage it in the Home Assistant UI: Settings > Devices & services > "
    + "“HA-MCP — Manage Home Assistant with AI” (listed as “HA-MCP Custom Component” "
    + "on older installs). Choose Configure for options; use the entry's menu for "
    + "enable/disable, delete and debug logging.",
    "Server settings and updates: with developer mode on, "
    + "ha_dev_manage_settings / ha_dev_manage_server.",
)


def is_component_domain(domain: object) -> bool:
    """Whether ``domain`` names ha-mcp's own ``ha_mcp_tools`` integration.

    Core's integration lookup (config flows, log levels) is case-sensitive
    today, so there a mixed-case or padded domain fails downstream anyway and
    the normalisation is defence in depth. Core's service registry does
    lowercase a domain, which is why ha_call_service's guard needs it.
    """
    return isinstance(domain, str) and domain.strip().lower() == COMPONENT_DOMAIN


def refuse_component_entry(
    domain: object, *, action: str, entry_id: str | None = None
) -> None:
    """Refuse ``action`` on a config entry of ha-mcp's own custom component.

    Writing one of those entries reloads or alters the server answering the
    call. The HA UI owns them, and the ``ha_dev_*`` tools defer writes that
    would reload the running server (embedded mode), so the generic
    integration tools must not become a bypass.
    """
    if not is_component_domain(domain):
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Cannot {action} ha-mcp's own '{COMPONENT_DOMAIN}' integration "
            "with this tool: its config entries are the server's own and are "
            "not managed through the generic integration tools.",
            suggestions=list(COMPONENT_ENTRY_SUGGESTIONS),
            context={"entry_id": entry_id, "domain": domain},
        )
    )


async def _restore_config_entry_options(
    client: Any,
    entry_id: str,
    expected_domain: str | None,
    progress: _OptionsFlowProgress,
) -> dict[str, Any]:
    _reject_redaction_sentinels(progress.config)
    config_entry = await config_entry_of_domain(client, entry_id, expected_domain)
    actual_domain = config_entry.get("domain")
    refuse_component_entry(
        actual_domain, action="change the options of", entry_id=entry_id
    )
    refuse_ignored_entry(
        config_entry, action="change the options of", entry_id=entry_id
    )

    flow_result = await client.start_options_flow(entry_id)
    flow_id = flow_result.get("flow_id")
    progress.flow_id = flow_id
    if flow_result.get("type") == _FlowType.CREATE_ENTRY:
        progress.apply_status = "applied"

    if not flow_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to start options flow",
                suggestions=[
                    "Check that the entry supports options (supports_options=true)"
                ],
                context={"entry_id": entry_id, "details": flow_result},
            )
        )

    try:
        progress.start(flow_result)
        result = await replay_snapshot(client, progress, flow_result)
        if result.get("type") != _FlowType.CREATE_ENTRY:
            raise progress.failure()
    except (Exception, asyncio.CancelledError) as flow_err:
        if progress.apply_status == "not_applied":
            await abort_failed_flow_type_only(
                client.abort_options_flow,
                flow_id,
                "Failed to abort options flow %s for entry %s "
                "(stage=abort_cleanup, reason=%s, error_type=%s)",
                flow_id,
                entry_id,
                flow_err.reason if isinstance(flow_err, OptionsFlowError) else None,
            )
        raise

    return {
        "success": True,
        "entry_id": entry_id,
        "domain": actual_domain,
        "updated": True,
    }
