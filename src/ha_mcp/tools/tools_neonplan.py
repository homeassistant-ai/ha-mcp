"""Read saved NeonPlan geometry and device positions through Home Assistant."""

from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import HomeAssistantAPIError
from ..client.websocket_client import get_websocket_client
from .helpers import log_tool_usage, register_tool_methods
from .tool_hints import read_only_hints


def _contains(points: list[list[float]], x: float, z: float) -> bool:
    """Return whether the point is inside or on the boundary of the room."""
    inside = False
    for (x1, z1), (x2, z2) in zip(points, points[1:] + points[:1]):
        if (
            min(x1, x2) <= x <= max(x1, x2)
            and min(z1, z2) <= z <= max(z1, z2)
            and abs((x - x1) * (z2 - z1) - (z - z1) * (x2 - x1)) < 1e-8
        ):
            return True
        if (z1 > z) != (z2 > z) and x < x1 + (z - z1) * (x2 - x1) / (z2 - z1):
            inside = not inside
    return inside


class NeonPlanTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_neonplan",
        tags={"Integrations"},
        annotations=read_only_hints("Read the saved NeonPlan floor plan", open_world=False),
    )
    @log_tool_usage
    async def ha_get_neonplan(
        self,
        plan: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")] = "main",
        entity_id: Annotated[
            str | None, Field(description="Exact Home Assistant entity ID to locate.")
        ] = None,
    ) -> dict[str, Any]:
        """Read live NeonPlan floor and room geometry, or locate a placed entity.

        Supply an entity ID to return only its saved positions. Room matches
        are based on geometry and may be ambiguous on shared boundaries.
        Coordinates and elevations are in metres in NeonPlan's coordinate system.
        """
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            response = await ws.send_command("neonplan3d/building/get", plan=plan)
        except (HomeAssistantAPIError, TimeoutError, OSError) as exc:
            raise ToolError("Could not read NeonPlan through Home Assistant.") from exc

        if not response.get("success"):
            error_code = (response.get("error") or {}).get("code", "unknown")
            raise ToolError(f"Home Assistant refused the NeonPlan request ({error_code}).")
        result = response["result"]
        floors = result["building"]["floors"]
        metadata = {"plan": result["plan"], "revision": result["revision"]}

        if entity_id is None:
            return {
                **metadata,
                "floors": [
                    {
                        "id": floor["id"],
                        "name": floor["name"],
                        "elevation": floor["elevation"],
                        "rooms": [
                            {key: room[key] for key in ("id", "name", "area_id", "points")}
                            for room in floor["rooms"]
                        ],
                    }
                    for floor in floors
                ],
            }

        matches = []
        for floor in floors:
            for placement in floor["placements"]:
                if placement["entity_id"] != entity_id:
                    continue
                x, z = placement["x"], placement["z"]
                matches.append({
                    "floor": floor["name"],
                    "floor_id": floor["id"],
                    "rooms": [
                        {"id": room["id"], "name": room["name"]}
                        for room in floor["rooms"]
                        if _contains(room["points"], x, z)
                    ],
                    "position": {"x": x, "y": placement["y"], "z": z},
                })
        return {**metadata, "entity_id": entity_id, "placements": matches}


def register_neonplan_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the NeonPlan tool on the existing HA-MCP server."""
    register_tool_methods(mcp, NeonPlanTools(client))
