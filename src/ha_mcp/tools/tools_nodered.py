"""
Node-RED flow management tools for the Home Assistant MCP server.

These tools wrap the Node-RED Admin API (HTTP Basic auth, exposed via the
HA Node-RED add-on's NGINX proxy by default) so AI agents can read flows,
patch nodes, and deploy structural changes from the same MCP session that
controls Home Assistant itself.

Feature flag: set ``ENABLE_NODERED_TOOLS=true`` along with ``NODERED_URL``,
``NODERED_USERNAME`` and ``NODERED_PASSWORD`` to load these tools. The
module is a no-op when the flag is off.

Behaviour notes preserved from the prior hand-rolled server:
- ``ha_nodered_patch_node`` and ``ha_nodered_patch_flow`` only mutate
  *existing* node properties — they cannot change a node's type. Use
  ``ha_nodered_replace_flow`` for structural rewrites.
- ``ha_nodered_replace_flow`` requires the caller to supply the full new
  list of nodes for the tab. Disabled nodes must carry ``"d": true``
  explicitly; there is no additive-insert mode.
- ``ha_nodered_get_nodes`` filters by ``search_name`` (case-insensitive
  substring on ``name``) and/or ``node_type``.
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.nodered_client import NodeRedClient
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response, create_resource_not_found_error
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

# Cap returned matches from search-style tools so a misconfigured Node-RED
# install (thousands of nodes) cannot blow up an MCP response payload.
_NODE_SEARCH_LIMIT = 100


class NodeRedTools:
    """Node-RED Admin API tools."""

    def __init__(self, client: NodeRedClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Read-only tools
    # ------------------------------------------------------------------

    @tool(
        name="ha_nodered_get_flows",
        tags={"Node-RED"},
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "List Node-RED Flows",
        },
    )
    @log_tool_usage
    async def ha_nodered_get_flows(self) -> dict[str, Any]:
        """List all Node-RED flow tabs with per-tab node counts."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"operation": "nodered_get_flows"},
                suggestions=[
                    "Verify NODERED_URL is reachable",
                    "Confirm NODERED_USERNAME / NODERED_PASSWORD are correct",
                ],
            )

        tabs: list[dict[str, Any]] = []
        nodes_per_tab: dict[str, int] = {}
        config_node_count = 0

        for node in flows:
            node_type = node.get("type", "")
            if node_type == "tab":
                tab_id = node.get("id", "")
                tabs.append(
                    {
                        "id": tab_id,
                        "label": node.get("label", "Unnamed"),
                        "disabled": node.get("disabled", False),
                        "info": node.get("info", ""),
                    }
                )
                nodes_per_tab.setdefault(tab_id, 0)
            elif node.get("z"):
                tab_id = node["z"]
                nodes_per_tab[tab_id] = nodes_per_tab.get(tab_id, 0) + 1
            else:
                config_node_count += 1

        return {
            "success": True,
            "data": {
                "total_tabs": len(tabs),
                "total_nodes": len(flows),
                "tabs": tabs,
                "nodes_per_tab": nodes_per_tab,
                "config_nodes_count": config_node_count,
            },
        }

    @tool(
        name="ha_nodered_get_flow",
        tags={"Node-RED"},
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Get Node-RED Flow",
        },
    )
    @log_tool_usage
    async def ha_nodered_get_flow(
        self,
        flow_id: Annotated[
            str,
            Field(description="The flow/tab ID to retrieve"),
        ],
    ) -> dict[str, Any]:
        """Get one Node-RED flow tab and every node it contains."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_get_flow", "flow_id": flow_id}
            )

        tab_info: dict[str, Any] | None = None
        tab_nodes: list[dict[str, Any]] = []
        for node in flows:
            if node.get("id") == flow_id and node.get("type") == "tab":
                tab_info = node
            elif node.get("z") == flow_id:
                tab_nodes.append(node)

        if tab_info is None:
            raise_tool_error(create_resource_not_found_error("flow", flow_id))

        return {
            "success": True,
            "data": {
                "id": tab_info.get("id"),
                "label": tab_info.get("label", "Unnamed"),
                "disabled": tab_info.get("disabled", False),
                "info": tab_info.get("info", ""),
                "node_count": len(tab_nodes),
                "nodes": tab_nodes,
            },
        }

    @tool(
        name="ha_nodered_get_nodes",
        tags={"Node-RED"},
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Search Node-RED Nodes",
        },
    )
    @log_tool_usage
    async def ha_nodered_get_nodes(
        self,
        node_type: Annotated[
            str | None,
            Field(
                description=(
                    "Exact node type to match (e.g. 'inject', 'function', "
                    "'api-call-service'). Case-sensitive."
                ),
                default=None,
            ),
        ] = None,
        search_name: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive substring matched against the node's "
                    "'name' property."
                ),
                default=None,
            ),
        ] = None,
        flow_id: Annotated[
            str | None,
            Field(
                description="Restrict the search to nodes inside this flow/tab.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Search Node-RED nodes across all flows by type, name and/or flow."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(e, context={"operation": "nodered_get_nodes"})

        needle = search_name.lower() if search_name else None
        matches: list[dict[str, Any]] = []
        for node in flows:
            if node.get("type") == "tab":
                continue
            if flow_id and node.get("z") != flow_id:
                continue
            if node_type and node.get("type") != node_type:
                continue
            if needle and needle not in node.get("name", "").lower():
                continue

            matches.append(
                {
                    "id": node.get("id"),
                    "type": node.get("type"),
                    "name": node.get("name", ""),
                    "flow_id": node.get("z", "global"),
                    "x": node.get("x"),
                    "y": node.get("y"),
                    "wires": node.get("wires", []),
                }
            )

        truncated = len(matches) > _NODE_SEARCH_LIMIT
        return {
            "success": True,
            "data": {
                "filters": {
                    "node_type": node_type,
                    "search_name": search_name,
                    "flow_id": flow_id,
                },
                "matches": len(matches),
                "truncated": truncated,
                "limit": _NODE_SEARCH_LIMIT,
                "nodes": matches[:_NODE_SEARCH_LIMIT],
            },
        }

    @tool(
        name="ha_nodered_get_settings",
        tags={"Node-RED"},
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Get Node-RED Runtime Settings",
        },
    )
    @log_tool_usage
    async def ha_nodered_get_settings(self) -> dict[str, Any]:
        """Get Node-RED runtime settings (version, palette categories, theme)."""
        try:
            settings = await self._client.get_settings()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_get_settings"}
            )

        return {
            "success": True,
            "data": {
                "version": settings.get("version", ""),
                "http_node_root": settings.get("httpNodeRoot", "/"),
                "palette_categories": settings.get("paletteCategories", []),
                "flow_encryption": settings.get("flowEncryptionType", ""),
                "editor_theme": settings.get("editorTheme", {}),
            },
        }

    # ------------------------------------------------------------------
    # State-changing tools
    # ------------------------------------------------------------------

    @tool(
        name="ha_nodered_inject_node",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": False,
            "idempotentHint": False,
            "title": "Trigger Node-RED Inject Node",
        },
    )
    @log_tool_usage
    async def ha_nodered_inject_node(
        self,
        node_id: Annotated[str, Field(description="ID of the inject node to trigger")],
    ) -> dict[str, Any]:
        """Trigger a Node-RED inject node by ID."""
        try:
            await self._client.inject(node_id)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"operation": "nodered_inject_node", "node_id": node_id},
            )

        return {
            "success": True,
            "data": {"node_id": node_id, "message": "Inject node triggered"},
        }

    @tool(
        name="ha_nodered_patch_node",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Patch Node-RED Node",
        },
    )
    @log_tool_usage
    async def ha_nodered_patch_node(
        self,
        node_id: Annotated[str, Field(description="ID of the node to patch")],
        patches: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "Properties to overwrite on the node, e.g. "
                    "{'func': 'return msg;', 'name': 'My Name'}. Cannot "
                    "change the node's 'type' — use ha_nodered_replace_flow "
                    "for structural changes."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Patch one node's properties in place and redeploy all flows."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"operation": "nodered_patch_node", "node_id": node_id},
            )

        target: dict[str, Any] | None = None
        for node in flows:
            if node.get("id") == node_id:
                target = node
                break

        if target is None:
            raise_tool_error(create_resource_not_found_error("node", node_id))

        if "type" in patches and patches["type"] != target.get("type"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Cannot change node 'type' via patch; use ha_nodered_replace_flow.",
                    context={"node_id": node_id, "current_type": target.get("type")},
                )
            )

        for key, value in patches.items():
            target[key] = value

        try:
            revision = await self._client.post_flows(flows)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"operation": "nodered_patch_node", "node_id": node_id},
            )

        return {
            "success": True,
            "data": {
                "message": "Node patched and flows deployed",
                "node": {
                    "id": target.get("id"),
                    "type": target.get("type"),
                    "name": target.get("name", ""),
                    "patched_fields": list(patches.keys()),
                },
                "revision": revision if isinstance(revision, str) else None,
            },
        }

    @tool(
        name="ha_nodered_patch_flow",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Patch Multiple Node-RED Nodes",
        },
    )
    @log_tool_usage
    async def ha_nodered_patch_flow(
        self,
        flow_id: Annotated[
            str, Field(description="ID of the flow/tab containing the nodes to patch")
        ],
        node_patches: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "List of patch specs, each shaped "
                    "{'node_id': '...', 'patches': {field: value, ...}}. "
                    "All target nodes must already exist in the named flow; "
                    "node 'type' cannot be changed."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Patch multiple nodes in one flow and redeploy all flows."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_patch_flow", "flow_id": flow_id}
            )

        flow_exists = any(
            n.get("id") == flow_id and n.get("type") == "tab" for n in flows
        )
        if not flow_exists:
            raise_tool_error(create_resource_not_found_error("flow", flow_id))

        nodes_by_id = {node.get("id"): node for node in flows}
        patched: list[dict[str, Any]] = []
        item_errors: list[dict[str, Any]] = []

        for patch_spec in node_patches:
            node_id = patch_spec.get("node_id")
            patches = patch_spec.get("patches", {})

            if not node_id or node_id not in nodes_by_id:
                item_errors.append(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Node '{node_id}' not found",
                        context={"node_id": node_id},
                    )
                )
                continue

            node = nodes_by_id[node_id]
            if node.get("z") != flow_id:
                item_errors.append(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Node '{node_id}' is not in flow '{flow_id}'",
                        context={"node_id": node_id, "flow_id": flow_id},
                    )
                )
                continue

            if "type" in patches and patches["type"] != node.get("type"):
                item_errors.append(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Cannot change 'type' on node '{node_id}'",
                        context={"node_id": node_id},
                    )
                )
                continue

            for key, value in patches.items():
                node[key] = value
            patched.append(
                {
                    "id": node_id,
                    "type": node.get("type"),
                    "name": node.get("name", ""),
                    "patched_fields": list(patches.keys()),
                }
            )

        if not patched:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "No nodes were patched.",
                    context={"flow_id": flow_id, "errors": item_errors},
                )
            )

        try:
            revision = await self._client.post_flows(flows)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_patch_flow", "flow_id": flow_id}
            )

        return {
            "success": True,
            "data": {
                "message": f"Patched {len(patched)} node(s) and deployed",
                "patched_nodes": patched,
                "errors": item_errors or None,
                "revision": revision if isinstance(revision, str) else None,
            },
        }

    @tool(
        name="ha_nodered_replace_flow",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Replace Node-RED Flow Contents",
        },
    )
    @log_tool_usage
    async def ha_nodered_replace_flow(
        self,
        flow_id: Annotated[
            str, Field(description="ID of the flow/tab to replace contents of")
        ],
        new_flow_nodes: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Complete list of new node objects for this flow. "
                    "Caller is responsible for the FULL set of nodes — there "
                    "is no additive insert mode. Disabled nodes must carry "
                    "'d': true explicitly. Each node's 'z' field is forced to "
                    "match flow_id."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Replace every node inside one flow tab; other flows are left intact."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_replace_flow", "flow_id": flow_id}
            )

        flow_tab: dict[str, Any] | None = None
        for node in flows:
            if node.get("id") == flow_id and node.get("type") == "tab":
                flow_tab = node
                break
        if flow_tab is None:
            raise_tool_error(create_resource_not_found_error("flow", flow_id))

        kept: list[dict[str, Any]] = []
        old_node_count = 0
        for node in flows:
            if node.get("z") == flow_id:
                old_node_count += 1
            else:
                kept.append(node)

        for node in new_flow_nodes:
            if node.get("type") != "tab":
                node["z"] = flow_id

        kept.extend(new_flow_nodes)

        try:
            revision = await self._client.post_flows(kept)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_replace_flow", "flow_id": flow_id}
            )

        return {
            "success": True,
            "data": {
                "message": f"Replaced flow '{flow_tab.get('label', flow_id)}'",
                "flow_id": flow_id,
                "flow_label": flow_tab.get("label", ""),
                "old_node_count": old_node_count,
                "new_node_count": len(new_flow_nodes),
                "revision": revision if isinstance(revision, str) else None,
            },
        }

    @tool(
        name="ha_nodered_add_flow",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": False,
            "idempotentHint": False,
            "title": "Add Node-RED Flow",
        },
    )
    @log_tool_usage
    async def ha_nodered_add_flow(
        self,
        flow_tab: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "The new tab node object — must include 'id', "
                    "'type': 'tab', and 'label'."
                )
            ),
        ],
        flow_nodes: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Nodes to place inside the new tab. Each node's 'z' "
                    "field is forced to match flow_tab['id']."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Create a new Node-RED flow tab with its nodes; other flows are kept."""
        if flow_tab.get("type") != "tab":
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "flow_tab must have type='tab'",
                    context={"flow_tab": flow_tab},
                )
            )
        if not flow_tab.get("id"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "flow_tab must have an 'id'",
                    context={"flow_tab": flow_tab},
                )
            )
        if not flow_tab.get("label"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "flow_tab must have a 'label'",
                    context={"flow_tab": flow_tab},
                )
            )

        flow_id = flow_tab["id"]

        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_add_flow", "flow_id": flow_id}
            )

        for node in flows:
            if node.get("id") == flow_id:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_ALREADY_EXISTS,
                        f"A node with ID '{flow_id}' already exists. "
                        "Use ha_nodered_replace_flow to update it.",
                        context={"flow_id": flow_id},
                    )
                )

        for node in flow_nodes:
            node["z"] = flow_id

        flows.append(flow_tab)
        flows.extend(flow_nodes)

        try:
            revision = await self._client.post_flows(flows)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_add_flow", "flow_id": flow_id}
            )

        return {
            "success": True,
            "data": {
                "message": f"Added new flow '{flow_tab.get('label')}'",
                "flow_id": flow_id,
                "flow_label": flow_tab.get("label"),
                "node_count": len(flow_nodes),
                "revision": revision if isinstance(revision, str) else None,
            },
        }

    @tool(
        name="ha_nodered_delete_flow",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Delete Node-RED Flow",
        },
    )
    @log_tool_usage
    async def ha_nodered_delete_flow(
        self,
        flow_id: Annotated[str, Field(description="ID of the flow/tab to delete")],
    ) -> dict[str, Any]:
        """Delete a Node-RED flow tab and every node inside it."""
        try:
            flows = await self._client.get_flows()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_delete_flow", "flow_id": flow_id}
            )

        flow_tab: dict[str, Any] | None = None
        for node in flows:
            if node.get("id") == flow_id and node.get("type") == "tab":
                flow_tab = node
                break
        if flow_tab is None:
            raise_tool_error(create_resource_not_found_error("flow", flow_id))

        deleted_count = 0
        kept: list[dict[str, Any]] = []
        for node in flows:
            if node.get("id") == flow_id or node.get("z") == flow_id:
                deleted_count += 1
            else:
                kept.append(node)

        try:
            revision = await self._client.post_flows(kept)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_delete_flow", "flow_id": flow_id}
            )

        return {
            "success": True,
            "data": {
                "message": f"Deleted flow '{flow_tab.get('label', flow_id)}'",
                "flow_id": flow_id,
                "flow_label": flow_tab.get("label", ""),
                "deleted_node_count": deleted_count,
                "revision": revision if isinstance(revision, str) else None,
            },
        }

    @tool(
        name="ha_nodered_update_flows",
        tags={"Node-RED"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Replace All Node-RED Flows",
        },
    )
    @log_tool_usage
    async def ha_nodered_update_flows(
        self,
        flows: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Complete /flows array (tabs, nodes and config nodes). "
                    "This REPLACES every flow. The caller must include all "
                    "existing content they want to keep."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Replace the entire Node-RED `/flows` array (full deployment)."""
        try:
            revision = await self._client.post_flows(flows)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e, context={"operation": "nodered_update_flows"}
            )

        return {
            "success": True,
            "data": {
                "message": "Flows deployed successfully",
                "node_count": len(flows),
                "revision": revision if isinstance(revision, str) else None,
            },
        }


def register_nodered_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Node-RED tools when ENABLE_NODERED_TOOLS=true.

    The ``client`` argument from the registry is the Home Assistant client and
    is intentionally ignored — Node-RED has its own credentials and HTTP
    client lifecycle.
    """
    settings = get_global_settings()
    if not settings.enable_nodered_tools:
        logger.debug(
            "Node-RED tools disabled (set ENABLE_NODERED_TOOLS=true to enable)"
        )
        return

    nodered_client = NodeRedClient(
        base_url=settings.nodered_url,
        username=settings.nodered_username,
        password=settings.nodered_password,
        timeout=settings.timeout,
    )
    register_tool_methods(mcp, NodeRedTools(nodered_client))
    logger.info("Node-RED tools registered (11 tools)")
