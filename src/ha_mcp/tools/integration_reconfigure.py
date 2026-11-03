"""The ``ha_set_integration`` reconfigure mode.

Split out of ``tools_integrations.py`` when that module passed 3,500 lines.
Holds the whole reconfigure surface: the parameters legal only in this mode,
the confirm-token handshake, the preflight response, and the
runner that drives ``config_entry_reconfigure.reconfigure_config_entry``.

``tools_integrations`` keeps the ``ha_set_integration`` tool itself and
delegates the reconfigure branch here. Deliberately NOT named ``tools_*``:
the registry auto-discovers that prefix expecting a ``register_*_tools``
function, and ``test_tool_module_naming`` enforces it.
"""

import hmac
import logging
from typing import Any, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .auto_backup import with_auto_backup
from .config_entry_flow_errors import ReconfigureStatus
from .config_entry_identity import ReconfigureIdentity
from .config_entry_reconfigure import (
    PreparedReconfigure,
    prepare_reconfigure_request,
    reconfigure_config_entry,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .integration_flow_steps import (
    FlowTarget,
    abort_step_flow,
    read_step,
    reconfigure_step_response,
    settle,
    start_reconfigure_step,
)

logger = logging.getLogger(__name__)


#: The ``ha_set_integration`` parameters that are legal only in reconfigure
#: mode. Declared once so the guard below, its error message, the tool
#: docstring and ``test_tools_integrations`` cannot drift apart.
RECONFIGURE_ONLY_PARAMETERS: tuple[str, ...] = (
    "confirm_token",
    "expected_device_id",
    "expected_entity_ids",
    "expected_mac",
    "expected_unique_id",
)


def reject_reconfigure_only_parameters(
    *,
    confirm_token: str | None,
    expected_device_id: str | None,
    expected_unique_id: str | None,
    expected_mac: str | None,
    expected_entity_ids: list[str] | None,
) -> None:
    """Reject the confirmation and identity guards outside reconfigure mode."""
    supplied: dict[str, Any] = {
        "confirm_token": confirm_token,
        "expected_device_id": expected_device_id,
        "expected_entity_ids": expected_entity_ids,
        "expected_mac": expected_mac,
        "expected_unique_id": expected_unique_id,
    }
    rejected = [
        name for name in RECONFIGURE_ONLY_PARAMETERS if supplied.get(name) is not None
    ]
    if not rejected:
        return

    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "These parameters require reconfigure=True and were rejected: "
            + ", ".join(rejected),
            suggestions=[
                "Pass reconfigure=True to use the confirmation and identity "
                "safeguards of the official reconfigure flow."
            ],
            # Not "ignored": the call was rejected and nothing ran.
            context={"rejected_parameters": rejected},
        )
    )


def _reconfigure_preflight_token(
    *,
    entry: dict[str, Any],
    flow_id: str,
    expected_identity: dict[str, Any],
    identity: ReconfigureIdentity,
) -> str:
    """Bind confirmation to stable entry identity and the open flow.

    This is an unkeyed content hash — an optimistic lock, not a cryptographic
    binding. It exists so a confirmation cannot apply against state the caller
    was never shown.

    It covers the DISCOVERED identity, not just the anchors the caller typed.
    Most callers supply no ``expected_*`` at all and rely on what the preview
    showed them; if the entry's devices, entities or MACs move between preview
    and confirm, hashing only the caller's (empty) anchors would leave the
    token valid and apply against associations the caller never approved.

    Runtime state and error fields are deliberately excluded: Home Assistant
    changes them while an entry reloads, and an offline device flapping
    between ``setup_retry`` and ``setup_in_progress`` would otherwise make
    every confirmation stale with no way forward.
    """
    payload = {
        "entry_id": entry.get("entry_id"),
        "domain": entry.get("domain"),
        # From the collected identity, not the entry: Home Assistant's
        # config-entry fragment has no unique_id, so entry.get would hash a
        # constant None and the token would not bind to it at all.
        "unique_id": identity.unique_id,
        "unique_id_known": identity.unique_id_known,
        "title": entry.get("title"),
        "flow_id": flow_id,
        "expected_identity": expected_identity,
        # The identity the preview SHOWED the caller.
        "discovered_device_ids": identity.device_ids,
        "discovered_entity_ids": identity.entity_ids,
        "discovered_macs": identity.macs,
    }
    return f"sha256:{compute_config_hash(payload)}"


class ReconfigureRunner:
    """Drives one ``ha_set_integration(reconfigure=True)`` request.

    A class rather than plain functions because ``with_auto_backup`` resolves
    its client from ``self._client``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @with_auto_backup(
        domain="integration",
        id_param="entry_id",
    )
    async def _apply_after_confirmation(
        self,
        entry_id: str,
        *,
        prepared: PreparedReconfigure,
        flow_id: str,
        menu_choice: str | None,
    ) -> dict[str, Any]:
        """Capture the normal edit backup only after confirmation validation."""
        return await reconfigure_config_entry(
            self._client,
            entry_id,
            prepared=prepared,
            flow_id=flow_id,
            menu_choice=menu_choice,
        )

    async def _open_step(
        self,
        prepared: PreparedReconfigure,
        target: FlowTarget,
        flow_id: str,
        step: dict[str, Any],
    ) -> dict[str, Any]:
        """The step the flow is open at, as the answer. Reached without a
        submit, so a flow found finished may still have changed the entry."""
        if step.get("type") not in ("form", "menu"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The reconfigure flow finished without a submit from this call",
                    suggestions=["Check the entry in Home Assistant before retrying."],
                    context={
                        "entry_id": prepared.entry_id,
                        "flow_id": flow_id,
                        "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                        "details": step,
                    },
                )
            )
        return await reconfigure_step_response(
            self._client, target, flow_id, step, prepared.entry, prepared.warnings
        )

    async def _start(
        self, prepared: PreparedReconfigure, target: FlowTarget, *, answer_given: bool
    ) -> dict[str, Any]:
        """Open the reconfigure flow and return its first step with the token
        every later call to this flow needs. Nothing is submitted."""
        flow_id, step = await start_reconfigure_step(self._client, target)
        response = await self._open_step(prepared, target, flow_id, step)
        response["confirm_token"] = _reconfigure_preflight_token(
            entry=prepared.entry,
            flow_id=flow_id,
            expected_identity=prepared.expected_identity,
            identity=prepared.identity,
        )
        response["identity"] = prepared.identity.as_payload()
        response["expected_identity"] = prepared.expected_identity
        response["message"] = (
            f"Checked: the entry exists, {target.domain} supports the official "
            "reconfigure flow, its identity anchors match and no duplicate "
            "entry shares its device. The flow is open at this step; nothing "
            "was submitted. Answer it with flow_id, confirm_token and "
            "config (or menu_choice), or end it with flow_id and abort=True."
        )
        if answer_given:
            response.setdefault("warnings", []).append(
                "config and menu_choice were not submitted: the first call "
                "only opens the flow. Repeat the answer with flow_id and "
                "confirm_token."
            )
        return response

    @staticmethod
    def _check_token(
        prepared: PreparedReconfigure, flow_id: str, confirm_token: str | None
    ) -> None:
        """Refuse a step whose token is missing or no longer matches the
        identity collected for this call."""
        context: dict[str, Any] = {"entry_id": prepared.entry_id, "flow_id": flow_id}
        # Deliberately no confirm_token in the errors: handing back the
        # current one would let a caller apply against state it was never
        # shown, which is what the token prevents.
        suggestions = [
            "End this flow with flow_id and abort=True, then start again "
            "without flow_id for a fresh token."
        ]
        if confirm_token is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "A reconfigure step needs the confirm_token the flow's "
                    "first call returned",
                    suggestions=suggestions,
                    context={
                        **context,
                        "status": ReconfigureStatus.CONFIRMATION_REQUIRED,
                    },
                )
            )
        current = _reconfigure_preflight_token(
            entry=prepared.entry,
            flow_id=flow_id,
            expected_identity=prepared.expected_identity,
            identity=prepared.identity,
        )
        # Compare as bytes: hmac.compare_digest raises TypeError on a
        # non-ASCII str operand, and a caller's junk token must land on the
        # stale-preflight rejection, not an INTERNAL_ERROR.
        if not hmac.compare_digest(
            confirm_token.encode("utf-8", "surrogatepass"),
            current.encode("utf-8", "surrogatepass"),
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "The reconfigure confirmation is stale; the entry's "
                    "identity or anchors no longer match the step this flow's "
                    "token was issued for",
                    suggestions=suggestions,
                    context={**context, "status": ReconfigureStatus.STALE_PREFLIGHT},
                )
            )

    async def run(
        self,
        entry_id: str,
        *,
        config: dict[str, Any] | None = None,
        flow_id: str | None = None,
        menu_choice: str | None = None,
        abort: bool = False,
        expected_device_id: str | None = None,
        expected_unique_id: str | None = None,
        expected_mac: str | None = None,
        expected_entity_ids: list[str] | None = None,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Change an existing integration's connection settings safely.

        Works only for entries whose integration implements Home Assistant's
        official ``async_step_reconfigure`` flow. Home Assistant keeps
        ownership of integration-specific validation and updates the existing
        entry in place, preserving its entry/device/entity relationships.

        Without ``flow_id`` the identity checks run, the flow is opened and
        its first step returned with a ``confirm_token``; nothing is
        submitted. Each later call answers one step and must carry
        ``flow_id`` and the token: the identity is collected again first, and
        a token no longer matching it is refused. Auto-backup is captured only
        after the token validates. The returned rollback metadata describes
        repeating the official flow with operator intervention; it does not
        promise automatic restoration of connection endpoints or credentials.
        """
        try:
            if abort:
                if flow_id is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "abort=True needs the flow_id to end",
                            context={"entry_id": entry_id},
                        )
                    )
                return await abort_step_flow(
                    self._client, FlowTarget("reconfigure", None, entry_id), flow_id
                )
            if flow_id is not None and config is not None and menu_choice is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Pass menu_choice or config, not both: each answers one step",
                        suggestions=[
                            "The flow is still open: answer its step with "
                            "flow_id, confirm_token and one of them."
                        ],
                        context={"entry_id": entry_id, "flow_id": flow_id},
                    )
                )
            expected_identity = {
                "device_id": expected_device_id,
                "unique_id": expected_unique_id,
                "mac": expected_mac,
                "entity_ids": list(expected_entity_ids or []),
            }
            prepared = await prepare_reconfigure_request(
                self._client,
                entry_id,
                config=config,
                expected_identity=expected_identity,
            )
            target = FlowTarget(
                "reconfigure", prepared.entry["domain"], prepared.entry_id
            )
            if flow_id is None:
                return await self._start(
                    prepared,
                    target,
                    answer_given=config is not None or menu_choice is not None,
                )
            self._check_token(prepared, flow_id, confirm_token)
            if config is None and menu_choice is None:
                # Read the step again: nothing is submitted, so no backup.
                step = await read_step(self._client, target, flow_id)
                response = await self._open_step(
                    prepared,
                    target,
                    flow_id,
                    await settle(self._client, target, flow_id, step),
                )
            else:
                response = cast(
                    dict[str, Any],
                    await self._apply_after_confirmation(
                        entry_id=prepared.entry_id,
                        prepared=prepared,
                        flow_id=flow_id,
                        menu_choice=menu_choice,
                    ),
                )
            if response.get("status") == ReconfigureStatus.PREVIEW:
                # Still open at a step: the same token continues it.
                response["confirm_token"] = confirm_token
            return response
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to reconfigure integration: %s", e)
            exception_to_structured_error(
                e,
                context={
                    "entry_id": entry_id,
                    "flow_id": flow_id,
                    "config_keys": sorted(config or {}),
                },
                suggestions=[
                    "Verify the entry ID and that the integration supports its official "
                    "reconfigure flow.",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    async def handle_mode(
        self,
        *,
        entry_id: str | None,
        domain: str | None,
        enabled: bool | None,
        describe: bool,
        config: dict[str, Any] | None,
        flow_id: str | None,
        menu_choice: str | None,
        abort: bool,
        expected_device_id: str | None,
        expected_unique_id: str | None,
        expected_mac: str | None,
        expected_entity_ids: list[str] | None,
        confirm_token: str | None,
    ) -> dict[str, Any]:
        """Validate and dispatch the reconfigure mode of ``ha_set_integration``."""
        if domain is not None or enabled is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Reconfigure mode requires entry_id; do not "
                    "combine it with domain or enabled",
                    context={
                        "entry_id": entry_id,
                        "domain": domain,
                        "enabled": enabled,
                    },
                )
            )
        if describe:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "reconfigure=True without flow_id already opens the flow "
                    "and submits nothing: drop describe",
                    context={"entry_id": entry_id},
                )
            )
        if entry_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Reconfigure mode requires an existing entry_id",
                    suggestions=[
                        "Use ha_get_integration() to find a valid config entry ID"
                    ],
                )
            )
        return await self.run(
            entry_id,
            config=config,
            flow_id=flow_id,
            menu_choice=menu_choice,
            abort=abort,
            expected_device_id=expected_device_id,
            expected_unique_id=expected_unique_id,
            expected_mac=expected_mac,
            expected_entity_ids=expected_entity_ids,
            confirm_token=confirm_token,
        )
