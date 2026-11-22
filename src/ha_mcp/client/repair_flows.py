"""A Repairs issue's fix flow over Core's REST views, as the Repairs dialog
drives it.
"""

from typing import Any, Protocol


class _FlowEndpoint(Protocol):
    """The slice of ``HomeAssistantClient`` these calls need."""

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Issue one REST request and return its decoded body."""


async def start_repair_flow(
    client: _FlowEndpoint, domain: str, issue_id: str
) -> dict[str, Any]:
    """Start the fix flow of a Repairs issue; Core answers its first step."""
    step: dict[str, Any] = await client._request(
        "POST", "/repairs/issues/fix", json={"handler": domain, "issue_id": issue_id}
    )
    return step


async def submit_repair_flow_step(
    client: _FlowEndpoint, flow_id: str, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit data for a fix flow step; Core answers the next step."""
    step: dict[str, Any] = await client._request(
        "POST", f"/repairs/issues/fix/{flow_id}", json=user_input
    )
    return step


async def abort_repair_flow(client: _FlowEndpoint, flow_id: str) -> dict[str, Any]:
    """Abort an in-progress fix flow; the issue stays."""
    reply: dict[str, Any] = await client._request(
        "DELETE", f"/repairs/issues/fix/{flow_id}"
    )
    return reply
