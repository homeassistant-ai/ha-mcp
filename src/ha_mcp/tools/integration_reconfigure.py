"""The parameters of ``ha_set_integration``'s reconfigure mode.

The reconfigure flow itself is answered step by step in
``integration_flow_steps``; the identity checks run in
``config_entry_reconfigure``. Deliberately NOT named ``tools_*``: the registry
auto-discovers that prefix expecting a ``register_*_tools`` function, and
``test_tool_module_naming`` enforces it.
"""

from typing import Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

#: The ``ha_set_integration`` parameters that are legal only in reconfigure
#: mode. Declared once so the guard below, its error message, the tool
#: docstring and ``test_tools_integrations`` cannot drift apart.
RECONFIGURE_ONLY_PARAMETERS: tuple[str, ...] = (
    "expected_device_id",
    "expected_entity_ids",
    "expected_mac",
    "expected_unique_id",
)


def reject_reconfigure_only_parameters(
    *,
    expected_device_id: str | None,
    expected_unique_id: str | None,
    expected_mac: str | None,
    expected_entity_ids: list[str] | None,
) -> None:
    """Reject the identity guards outside reconfigure mode."""
    supplied: dict[str, Any] = {
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
                "Pass reconfigure=True to use the identity safeguards of the "
                "official reconfigure flow."
            ],
            # Not "ignored": the call was rejected and nothing ran.
            context={"rejected_parameters": rejected},
        )
    )


def refuse_reconfigure_conflicts(
    entry_id: str | None, domain: str | None, enabled: bool | None
) -> str:
    """The entry_id a reconfigure call needs, refusing domain and enabled."""
    if domain is not None or enabled is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Reconfigure mode requires entry_id; do not "
                "combine it with domain or enabled",
                context={"entry_id": entry_id, "domain": domain, "enabled": enabled},
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
    return entry_id
