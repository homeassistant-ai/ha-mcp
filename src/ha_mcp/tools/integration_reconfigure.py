"""The ``ha_set_integration`` reconfigure mode.

Split out of ``tools_integrations.py`` when that module passed 3,500 lines.
Holds the whole reconfigure surface: the parameters legal only in this mode,
the confirm-token handshake, the read-only preflight response, and the
runner that drives ``config_entry_flow.reconfigure_config_entry``.

``tools_integrations`` keeps the ``ha_set_integration`` tool itself and
delegates the reconfigure branch here. Deliberately NOT named ``tools_*``:
the registry auto-discovers that prefix expecting a ``register_*_tools``
function, and ``test_tool_module_naming`` enforces it.
"""

import hmac
import logging
from typing import Any, cast

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .auto_backup import with_auto_backup
from .config_entry_flow import (
    PreparedReconfigure,
    build_reconfigure_rollback_metadata,
    prepare_reconfigure_request,
    reconfigure_config_entry,
)
from .config_entry_flow_walker import ReconfigureStatus
from .helpers import exception_to_structured_error, raise_tool_error

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
    target_config: dict[str, Any],
    expected_identity: dict[str, Any],
) -> str:
    """Bind confirmation to stable entry identity and requested values.

    This is an unkeyed content hash — an optimistic lock, not a cryptographic
    binding. It exists so a confirmation cannot apply against state the caller
    was never shown. Runtime state and error fields are deliberately excluded:
    Home Assistant changes them while an entry reloads, and an offline device
    flapping between ``setup_retry`` and ``setup_in_progress`` would otherwise
    make every confirmation stale with no way forward.
    """
    payload = {
        "entry_id": entry.get("entry_id"),
        "domain": entry.get("domain"),
        "unique_id": entry.get("unique_id"),
        "title": entry.get("title"),
        "target_config": target_config,
        "expected_identity": expected_identity,
    }
    return f"sha256:{compute_config_hash(payload)}"


def _reconfigure_preview_response(
    *,
    entry_id: str,
    domain: str,
    entry: dict[str, Any],
    target_config: dict[str, Any],
    identity: dict[str, Any],
    expected_identity: dict[str, Any],
    confirm_token: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Build the non-mutating response returned by reconfigure preflight.

    ``status`` alone says this changed nothing — there is no second `preview`
    flag saying the same thing.
    """
    response: dict[str, Any] = {
        "success": True,
        "status": ReconfigureStatus.PREVIEW,
        "operation": "reconfigure",
        "entry_id": entry_id,
        "domain": domain,
        "title": entry.get("title"),
        # Say what was actually checked. The flow is never started here, so
        # nothing has compared these config keys against the integration's
        # reconfigure form — wrong field names surface only on the confirm leg.
        "message": (
            f"Checked: the entry exists, {domain} supports the official "
            "reconfigure flow, its identity anchors match, and no duplicate "
            "entry shares its device. The config keys are NOT validated until "
            "you confirm. Nothing was applied."
        ),
        "confirm_token": confirm_token,
        "target_config": target_config,
        "identity": identity,
        "expected_identity": expected_identity,
        "supports_reconfigure": True,
        "rollback": build_reconfigure_rollback_metadata(entry_id, domain),
    }
    if warnings:
        response["warnings"] = warnings
    return response


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
    ) -> dict[str, Any]:
        """Capture the normal edit backup only after confirmation validation."""
        return await reconfigure_config_entry(
            self._client,
            entry_id,
            prepared=prepared,
        )

    async def run(
        self,
        entry_id: str,
        *,
        config: dict[str, Any] | None = None,
        expected_device_id: str | None = None,
        expected_unique_id: str | None = None,
        expected_mac: str | None = None,
        expected_entity_ids: list[str] | None = None,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """Change an existing integration's configuration safely.

        Works only for entries whose integration implements Home Assistant's
        official ``async_step_reconfigure`` flow. Home Assistant keeps
        ownership of integration-specific validation and updates the existing
        entry in place, preserving its entry/device/entity relationships.

        ``confirm_token`` alone is the handshake, matching
        ``ha_config_set_yaml``: omitted runs the read-only preflight and
        returns a token, supplied applies. Auto-backup follows the normal
        integration backup policy and is captured only after the token
        validates, so a rejected confirmation leaves no snapshot behind. The
        returned rollback metadata describes repeating the official flow with
        operator intervention; it does not promise automatic restoration of
        connection endpoints or credentials.
        """
        try:
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
            current_confirm_token = _reconfigure_preflight_token(
                entry=prepared.entry,
                target_config=prepared.flow_config,
                expected_identity=prepared.expected_identity,
            )
            if confirm_token is None:
                return _reconfigure_preview_response(
                    entry_id=prepared.entry_id,
                    domain=prepared.entry["domain"],
                    entry=prepared.entry,
                    target_config=prepared.flow_config,
                    identity=prepared.identity.as_payload(),
                    expected_identity=prepared.expected_identity,
                    confirm_token=current_confirm_token,
                    warnings=prepared.warnings,
                )
            # Compare as bytes: hmac.compare_digest raises TypeError on a
            # non-ASCII str operand, and a caller's junk token must land on
            # the stale-preflight rejection, not an INTERNAL_ERROR.
            if not hmac.compare_digest(
                confirm_token.encode("utf-8", "surrogatepass"),
                current_confirm_token.encode("utf-8", "surrogatepass"),
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "The reconfigure confirmation is stale; the entry or "
                        "the requested change no longer matches the preflight "
                        "this token was issued for",
                        suggestions=[
                            "Repeat the call without confirm_token to see a fresh "
                            "preflight, then confirm with the token it returns."
                        ],
                        # Deliberately no confirm_token here: handing back the
                        # current one would let a caller apply against state it
                        # was never shown, which is what the token prevents.
                        context={
                            "entry_id": entry_id,
                            "status": ReconfigureStatus.STALE_PREFLIGHT,
                        },
                    )
                )

            return cast(
                dict[str, Any],
                await self._apply_after_confirmation(
                    entry_id=prepared.entry_id,
                    prepared=prepared,
                ),
            )
        except ToolError:
            raise
        except Exception as e:
            logger.error("Failed to reconfigure integration: %s", e)
            exception_to_structured_error(
                e,
                context={
                    "entry_id": entry_id,
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
        config: dict[str, Any] | None,
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
                    "Reconfigure mode requires entry_id and config; do not "
                    "combine it with domain or enabled",
                    context={
                        "entry_id": entry_id,
                        "domain": domain,
                        "enabled": enabled,
                    },
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
            expected_device_id=expected_device_id,
            expected_unique_id=expected_unique_id,
            expected_mac=expected_mac,
            expected_entity_ids=expected_entity_ids,
            confirm_token=confirm_token,
        )
