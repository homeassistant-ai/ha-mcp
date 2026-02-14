"""
Tool Search Proxy for ha-mcp — progressive tool discovery via meta-tools.

Implements the server-side Tool Search Proxy pattern recommended by Anthropic
(https://www.anthropic.com/engineering/code-execution-with-mcp) and validated
by Speakeasy (https://www.speakeasy.com/blog/100x-token-reduction-dynamic-toolsets).

Instead of registering all tools with MCP (consuming ~35K tokens of idle context),
this module registers 3 lightweight meta-tools that let the LLM discover and
execute tools on demand:

  ha_find_tools(query)          — search by name/category, returns summaries
  ha_get_tool_details(tool_name) — returns full description + parameter schema
  ha_execute_tool(tool_name, args, tool_schema) — validates & dispatches

The tool_schema parameter on ha_execute_tool is required — the LLM must call
ha_get_tool_details first and pass its output, providing structural enforcement
that prevents hallucinated or uninformed tool calls (same principle as PR #616's
guide_response, but applied universally to all proxied tools).

Proxied tools are NOT registered with MCP. Their implementations, descriptions,
and schemas are stored in a server-side registry and returned on demand.
"""

import importlib
import inspect
import json
import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Modules whose tools should be routed through the proxy instead of
# registered directly with MCP. Add module names here to proxy them.
# Tools in these modules remain fully functional — they're just accessed
# via ha_execute_tool instead of direct MCP tool calls.
#
# MIGRATION ROADMAP (Phase 1 → Phase N):
# ──────────────────────────────────────
# Phase 1 (this PR): 10 niche tools from 5 modules
PROXY_MODULES: set[str] = {
    "tools_zones",           # 4 tools: ha_get_zone, ha_create_zone, ha_update_zone, ha_delete_zone
    "tools_labels",          # 3 tools: ha_config_get_label, ha_config_set_label, ha_config_remove_label
    "tools_addons",          # 1 tool:  ha_get_addon
    "tools_voice_assistant", # 1 tool:  ha_get_entity_exposure
    "tools_traces",          # 1 tool:  ha_get_automation_traces
}
# Phase 2: Move ~20 more tools (calendar, groups, todo, resources, camera, ...)
# Phase 3: Move ~30 more tools (filesystem, hacs, integrations, ...)
# Phase 4: Move remaining tools — only 3 meta-tools stay registered
#
# To migrate a module: add its name to PROXY_MODULES above. That's it.
# The module's tools stop being registered with MCP and become proxy-only.
# No changes to the tool module code are needed.


class ToolProxyRegistry:
    """Server-side registry of proxied tools.

    Stores tool metadata (name, description, parameters, annotations) and
    implementation references for tools that are NOT registered with MCP.
    The meta-tools query this registry to serve tool information on demand.
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        # Category mapping built from tool annotations/tags
        self._categories: dict[str, list[str]] = {}

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        annotations: dict[str, Any],
        implementation: Any,
        module: str,
    ) -> None:
        """Register a tool in the proxy registry (NOT with MCP)."""
        # Extract category from annotations tags or module name
        tags = annotations.get("tags", [])
        category = tags[0] if tags else module.replace("tools_", "")

        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "annotations": annotations,
            "implementation": implementation,
            "module": module,
            "category": category,
        }

        # Build category index
        if category not in self._categories:
            self._categories[category] = []
        self._categories[category].append(name)

        logger.debug(f"Proxy registry: registered {name} (category: {category})")

    def find_tools(self, query: str) -> list[dict[str, Any]]:
        """Search for tools by name, category, or keyword.

        Returns compact summaries suitable for LLM consumption.
        """
        query_lower = query.lower().strip()
        results = []

        for name, tool in self._tools.items():
            # Match on tool name
            if query_lower in name.lower():
                results.append(self._make_summary(tool))
                continue

            # Match on category
            if query_lower in tool["category"].lower():
                results.append(self._make_summary(tool))
                continue

            # Match on description keywords
            if query_lower in tool["description"].lower():
                results.append(self._make_summary(tool))
                continue

            # Match on annotation tags
            tags = tool["annotations"].get("tags", [])
            if any(query_lower in tag.lower() for tag in tags):
                results.append(self._make_summary(tool))
                continue

        return results

    def get_tool_details(self, tool_name: str) -> dict[str, Any] | None:
        """Get full details for a specific tool.

        Returns the complete description, parameter schema, annotations,
        and an example call format. This is what the LLM needs to
        construct a valid ha_execute_tool call.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return None

        # Build a clean parameter list for LLM consumption
        params_info = self._format_parameters(tool["parameters"])

        return {
            "tool_name": tool["name"],
            "description": tool["description"],
            "category": tool["category"],
            "parameters": params_info,
            "annotations": {
                k: v
                for k, v in tool["annotations"].items()
                if k in ("destructiveHint", "readOnlyHint", "idempotentHint", "title")
            },
            "schema_hash": self._schema_hash(tool_name),
        }

    def get_tool(self, tool_name: str) -> dict[str, Any] | None:
        """Get the full tool entry including implementation."""
        return self._tools.get(tool_name)

    def get_catalog(self) -> dict[str, list[str]]:
        """Get the full tool catalog grouped by category."""
        return dict(self._categories)

    def validate_schema(self, tool_name: str, provided_hash: str) -> bool:
        """Validate that the provided schema hash matches the real tool."""
        return provided_hash == self._schema_hash(tool_name)

    def _schema_hash(self, tool_name: str) -> str:
        """Generate a short hash to prove the LLM read the real schema.

        This is intentionally NOT cryptographic — it's a structural
        enforcement mechanism, not a security measure. The hash changes
        if the tool's parameters change, ensuring the LLM has current info.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return ""
        # Use a simple deterministic string based on param names + types
        params = tool["parameters"]
        param_keys = sorted(params.get("properties", {}).keys()) if isinstance(params, dict) else []
        required = sorted(params.get("required", [])) if isinstance(params, dict) else []
        fingerprint = f"{tool_name}:{','.join(param_keys)}:{','.join(required)}"
        # Simple hash — not crypto, just a fingerprint
        import hashlib

        return hashlib.md5(fingerprint.encode()).hexdigest()[:8]

    def _make_summary(self, tool: dict[str, Any]) -> dict[str, Any]:
        """Create a compact tool summary for search results."""
        # First line of description only
        desc = tool["description"].strip()
        first_line = desc.split("\n")[0].strip()

        # Build compact param summary
        params = tool["parameters"]
        param_names = list(params.get("properties", {}).keys()) if isinstance(params, dict) else []
        required = params.get("required", []) if isinstance(params, dict) else []

        return {
            "tool_name": tool["name"],
            "summary": first_line,
            "category": tool["category"],
            "is_destructive": tool["annotations"].get("destructiveHint", False),
            "parameters": param_names,
            "required_parameters": required,
        }

    def _format_parameters(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Format parameter schema into LLM-friendly list."""
        if not isinstance(params, dict):
            return []

        properties = params.get("properties", {})
        required = set(params.get("required", []))
        result = []

        for name, schema in properties.items():
            param_info: dict[str, Any] = {
                "name": name,
                "type": schema.get("type", "string"),
                "required": name in required,
            }
            if "description" in schema:
                param_info["description"] = schema["description"]
            if "default" in schema:
                param_info["default"] = schema["default"]
            if "enum" in schema:
                param_info["enum"] = schema["enum"]
            result.append(param_info)

        return result


def _extract_tool_metadata(func: Any) -> tuple[str, str, dict[str, Any]]:
    """Extract tool name, description, and parameter schema from a function.

    Reads the function's type hints and docstring to build the same
    metadata that FastMCP would generate during @mcp.tool() registration.
    """
    import typing

    name = func.__name__
    description = inspect.getdoc(func) or ""

    # Build parameter schema from type hints
    hints = typing.get_type_hints(func, include_extras=True)
    sig = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        hint = hints.get(param_name)
        if hint is None:
            continue

        # Extract Field metadata from Annotated types
        param_schema: dict[str, Any] = {"type": "string"}  # default

        # Check for Annotated[..., Field(...)]
        origin = getattr(hint, "__origin__", None)
        args = getattr(hint, "__args__", ())

        if origin is Annotated:
            base_type = args[0] if args else str
            for metadata in args[1:]:
                if isinstance(metadata, type(Field())):
                    field_info = metadata
                    if hasattr(field_info, "description") and field_info.description:
                        param_schema["description"] = field_info.description
                    if hasattr(field_info, "default") and field_info.default is not None:
                        param_schema["default"] = field_info.default
            # Map Python types to JSON schema types
            param_schema["type"] = _python_type_to_json(base_type)
        else:
            param_schema["type"] = _python_type_to_json(hint)

        properties[param_name] = param_schema

        # Check if required (no default value)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return name, description, schema


def _python_type_to_json(python_type: Any) -> str:
    """Map Python type hints to JSON Schema types."""
    import types

    origin = getattr(python_type, "__origin__", None)

    # Handle Union types (e.g., str | None)
    if origin is types.UnionType:
        args = [a for a in python_type.__args__ if a is not type(None)]
        if args:
            return _python_type_to_json(args[0])
        return "string"

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
    }
    return type_map.get(python_type, "string")


def discover_proxy_tools(
    mcp: Any,
    client: Any,
    proxy_modules: set[str],
    **kwargs: Any,
) -> ToolProxyRegistry:
    """Import proxy modules and extract tool metadata WITHOUT registering with MCP.

    This mirrors what ToolsRegistry.register_all_tools() does, but instead of
    calling @mcp.tool(), it captures the tool functions and their metadata
    into a ToolProxyRegistry for the meta-tools to serve.
    """
    from .registry import EXPLICIT_MODULES

    registry = ToolProxyRegistry()

    for module_name in proxy_modules:
        try:
            # Handle explicit modules (non tools_*.py naming)
            if module_name in EXPLICIT_MODULES:
                module = importlib.import_module(f".{module_name}", "ha_mcp.tools")
                func_name = EXPLICIT_MODULES[module_name]
                register_func = getattr(module, func_name, None)
            else:
                module = importlib.import_module(f".{module_name}", "ha_mcp.tools")
                # Find register function
                register_func = None
                for attr_name in dir(module):
                    if attr_name.startswith("register_") and attr_name.endswith("_tools"):
                        register_func = getattr(module, attr_name)
                        break

            if not register_func:
                logger.warning(f"Proxy: no register function found in {module_name}")
                continue

            # Use a mock MCP to capture tool registrations
            mock = _MockMCP()
            register_func(mock, client, **kwargs)

            # Transfer captured tools to the proxy registry
            for tool_info in mock.captured_tools:
                registry.register_tool(
                    name=tool_info["name"],
                    description=tool_info["description"],
                    parameters=tool_info["parameters"],
                    annotations=tool_info["annotations"],
                    implementation=tool_info["implementation"],
                    module=module_name,
                )

            logger.debug(
                f"Proxy: captured {len(mock.captured_tools)} tools from {module_name}"
            )

        except Exception as e:
            logger.error(f"Proxy: failed to capture tools from {module_name}: {e}")
            raise

    logger.info(
        f"Tool proxy registry: {registry.tool_count} tools in "
        f"{len(proxy_modules)} modules"
    )
    return registry


class _MockMCP:
    """Mock FastMCP server that captures tool registrations without actually registering.

    When a tool module calls @mcp.tool(), this mock captures the decorated function,
    its annotations, and metadata instead of registering it with the real MCP server.
    """

    def __init__(self) -> None:
        self.captured_tools: list[dict[str, Any]] = []

    def tool(self, **kwargs: Any) -> Any:
        """Capture the @mcp.tool() decorator call."""
        annotations = kwargs.get("annotations", {})

        def decorator(func: Any) -> Any:
            # Unwrap log_tool_usage decorator if present
            inner = func
            while hasattr(inner, "__wrapped__"):
                inner = inner.__wrapped__

            name, description, parameters = _extract_tool_metadata(inner)

            self.captured_tools.append({
                "name": name,
                "description": description,
                "parameters": parameters,
                "annotations": annotations,
                "implementation": func,  # Keep the decorated version for execution
            })
            return func

        return decorator


def register_proxy_tools(
    mcp: Any,
    client: Any,
    proxy_registry: ToolProxyRegistry,
    **kwargs: Any,
) -> None:
    """Register the 3 meta-tools with MCP.

    These are the only tools the LLM sees for proxied tool access:
    - ha_find_tools: discover tools by query/category
    - ha_get_tool_details: get full schema + description for a tool
    - ha_execute_tool: validate schema proof + dispatch to real implementation
    """

    # Build the capabilities catalog for the ha_find_tools description
    catalog = proxy_registry.get_catalog()
    catalog_lines = []
    for category, tool_names in sorted(catalog.items()):
        catalog_lines.append(f"  {category.upper()}: {', '.join(sorted(tool_names))}")
    catalog_text = "\n".join(catalog_lines)

    # Build the docstring for ha_find_tools with embedded catalog
    find_tools_doc = (
        "Search for available Home Assistant tools by name, category, or keyword.\n\n"
        "Returns matching tools with summaries and parameter lists.\n"
        "Use ha_get_tool_details(tool_name) to get full documentation before calling a tool.\n\n"
        f"AVAILABLE TOOLS ({proxy_registry.tool_count} proxied):\n"
        f"{catalog_text}\n\n"
        "To use any of these tools:\n"
        "1. Call ha_find_tools(query) to search (you are here)\n"
        "2. Call ha_get_tool_details(tool_name) to get full docs + schema\n"
        "3. Call ha_execute_tool(tool_name, args, tool_schema) to execute"
    )

    @mcp.tool(
        description=find_tools_doc,
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Find Tools",
            "tags": ["proxy"],
        },
    )
    @log_tool_usage
    async def ha_find_tools(
        query: Annotated[
            str,
            Field(
                description="Search query — tool name, category, or keyword. "
                "Examples: 'zone', 'label', 'addon', 'traces', 'voice'."
            ),
        ],
    ) -> dict[str, Any]:
        """Search for available Home Assistant tools."""
        results = proxy_registry.find_tools(query)

        if not results:
            return {
                "success": True,
                "matches": [],
                "count": 0,
                "message": f"No tools found matching '{query}'.",
                "available_categories": sorted(catalog.keys()),
                "suggestion": "Try searching by category name or a broader keyword.",
            }

        return {
            "success": True,
            "matches": results,
            "count": len(results),
            "message": f"Found {len(results)} tool(s) matching '{query}'.",
            "next_step": "Call ha_get_tool_details(tool_name) for full documentation.",
        }

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Get Tool Details",
            "tags": ["proxy"],
        }
    )
    @log_tool_usage
    async def ha_get_tool_details(
        tool_name: Annotated[
            str,
            Field(
                description="Exact tool name to get details for (from ha_find_tools results)."
            ),
        ],
    ) -> dict[str, Any]:
        """Get full documentation and parameter schema for a specific tool.

        Returns the complete tool description, all parameters with types
        and descriptions, and the schema_hash needed for ha_execute_tool.

        You MUST call this before ha_execute_tool — the schema_hash from
        this response is required to execute the tool.
        """
        details = proxy_registry.get_tool_details(tool_name)

        if not details:
            # Suggest similar tools
            all_tools = []
            for tools in catalog.values():
                all_tools.extend(tools)
            suggestions = [t for t in all_tools if tool_name.lower() in t.lower()][:5]

            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found in proxy registry.",
                "available_tools": sorted(all_tools),
                "suggestions": suggestions or "Use ha_find_tools() to search for tools.",
            }

        return {
            "success": True,
            **details,
            "usage": (
                f"Call ha_execute_tool(tool_name='{tool_name}', "
                f"args={{...}}, tool_schema='{details['schema_hash']}')"
            ),
        }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "title": "Execute Tool",
            "tags": ["proxy"],
        }
    )
    @log_tool_usage
    async def ha_execute_tool(
        tool_name: Annotated[
            str,
            Field(description="Tool name to execute (from ha_get_tool_details)."),
        ],
        args: Annotated[
            str,
            Field(
                description="JSON object of arguments to pass to the tool. "
                "Must match the parameter schema from ha_get_tool_details."
            ),
        ],
        tool_schema: Annotated[
            str,
            Field(
                description="Schema hash from ha_get_tool_details response "
                "(proves you read the tool documentation)."
            ),
        ],
    ) -> dict[str, Any]:
        """Execute a proxied tool with validated arguments.

        REQUIRED: You must call ha_get_tool_details(tool_name) first and pass
        the schema_hash from its response as tool_schema. This is required to
        ensure you have the current parameter documentation.

        The args parameter must be a JSON object matching the tool's parameter schema.
        The server validates both the schema_hash and the arguments before execution.
        """
        # Validate tool exists
        tool = proxy_registry.get_tool(tool_name)
        if not tool:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found.",
                "suggestion": "Use ha_find_tools() to search for available tools.",
            }

        # Validate schema hash — proves LLM read the docs
        if not proxy_registry.validate_schema(tool_name, tool_schema):
            return {
                "success": False,
                "error": "Invalid tool_schema hash. You must call "
                "ha_get_tool_details(tool_name) first and pass the "
                "schema_hash from the response.",
                "required_step": f"Call ha_get_tool_details('{tool_name}') to get the current schema_hash.",
            }

        # Parse args
        try:
            if isinstance(args, str):
                parsed_args = json.loads(args)
            elif isinstance(args, dict):
                parsed_args = args
            else:
                return {
                    "success": False,
                    "error": f"args must be a JSON object string, got {type(args).__name__}",
                }
        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error": f"Invalid JSON in args: {e}",
                "suggestion": "Ensure args is a valid JSON object string.",
            }

        if not isinstance(parsed_args, dict):
            return {
                "success": False,
                "error": "args must be a JSON object (not array or primitive).",
            }

        # Validate required parameters
        params_schema = tool["parameters"]
        required_params = set(params_schema.get("required", []))
        provided_params = set(parsed_args.keys())
        missing = required_params - provided_params

        if missing:
            # Build helpful error with parameter details
            details = proxy_registry.get_tool_details(tool_name)
            param_help = []
            if details:
                for p in details["parameters"]:
                    if p["name"] in missing:
                        param_help.append(
                            f"  - {p['name']} ({p['type']}, required): "
                            f"{p.get('description', 'no description')}"
                        )

            return {
                "success": False,
                "error": f"Missing required parameter(s): {', '.join(sorted(missing))}",
                "missing_parameters": sorted(missing),
                "parameter_help": param_help,
                "provided_parameters": sorted(provided_params),
            }

        # Execute the tool
        try:
            implementation = tool["implementation"]
            result = await implementation(**parsed_args)
            return result

        except TypeError as e:
            # Likely wrong parameter types
            error_msg = str(e)
            details = proxy_registry.get_tool_details(tool_name)
            return {
                "success": False,
                "error": f"Parameter error: {error_msg}",
                "tool_name": tool_name,
                "provided_args": parsed_args,
                "expected_parameters": details["parameters"] if details else [],
                "suggestion": "Check parameter types match the schema from ha_get_tool_details.",
            }
        except Exception as e:
            logger.error(f"Proxy execution error for {tool_name}: {e}")
            return {
                "success": False,
                "error": f"Tool execution failed: {str(e)}",
                "tool_name": tool_name,
            }
