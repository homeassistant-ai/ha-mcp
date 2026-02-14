"""
Tool Search Proxy — progressive tool discovery via 3 meta-tools.

Instead of registering all tools with MCP (~35K tokens of idle context),
proxied tools are discovered and executed on demand via:

  ha_find_tools(query)           — search by name/category
  ha_get_tool_details(tool_name) — full schema + description
  ha_execute_tool(tool_name, args, tool_schema) — validate & dispatch

See: https://www.anthropic.com/engineering/code-execution-with-mcp
"""

import hashlib
import importlib
import inspect
import json
import logging
import types
import typing
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import (
    ErrorCode,
    create_error_response,
    create_resource_not_found_error,
    create_validation_error,
)
from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Modules whose tools are proxied instead of registered directly with MCP.
# To proxy a module: add its name here.  No changes to the module code needed.
PROXY_MODULES: set[str] = {
    "tools_zones",
    "tools_labels",
    "tools_addons",
    "tools_voice_assistant",
    "tools_traces",
}


class ToolProxyRegistry:
    """Server-side registry of proxied tools and their metadata."""

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
        """Search for tools by name, category, or keyword."""
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
        """Get full description, parameter schema, and schema_hash for a tool."""
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
        """Short fingerprint of the tool's parameter schema."""
        tool = self._tools.get(tool_name)
        if not tool:
            return ""
        params = tool["parameters"]
        param_keys = sorted(params.get("properties", {}).keys()) if isinstance(params, dict) else []
        required = sorted(params.get("required", [])) if isinstance(params, dict) else []
        fingerprint = f"{tool_name}:{','.join(param_keys)}:{','.join(required)}"
        return hashlib.md5(fingerprint.encode()).hexdigest()[:8]

    def _make_summary(self, tool: dict[str, Any]) -> dict[str, Any]:
        params = tool["parameters"]
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        return {
            "tool_name": tool["name"],
            "summary": tool["description"].strip().split("\n")[0],
            "category": tool["category"],
            "is_destructive": tool["annotations"].get("destructiveHint", False),
            "parameters": list(props.keys()),
            "required_parameters": params.get("required", []) if isinstance(params, dict) else [],
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
    """Extract tool name, description, and parameter schema from type hints."""
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
    """Import proxy modules and capture tool metadata without MCP registration."""
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
    """Mock FastMCP that captures @mcp.tool() registrations without registering."""

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
    """Register the 3 meta-tools (find, details, execute) with MCP."""

    # Build the capabilities catalog for the ha_find_tools description
    catalog = proxy_registry.get_catalog()
    catalog_lines = []
    for category, tool_names in sorted(catalog.items()):
        catalog_lines.append(f"  {category.upper()}: {', '.join(sorted(tool_names))}")
    catalog_text = "\n".join(catalog_lines)

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
            similar = [t for t in all_tools if tool_name.lower() in t.lower()][:5]

            return create_resource_not_found_error(
                resource_type="Tool",
                identifier=tool_name,
                details=(
                    f"Tool '{tool_name}' is not in the proxy registry. "
                    f"Available tools: {', '.join(sorted(all_tools))}. "
                    + (f"Similar: {', '.join(similar)}. " if similar else "")
                    + "Use ha_find_tools() to search for tools."
                ),
            )

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
            return create_resource_not_found_error(
                resource_type="Tool",
                identifier=tool_name,
                details="Use ha_find_tools() to search for available tools.",
            )

        # Validate schema hash — proves LLM read the docs
        if not proxy_registry.validate_schema(tool_name, tool_schema):
            return create_validation_error(
                message=(
                    "Invalid tool_schema hash. You must call "
                    "ha_get_tool_details(tool_name) first and pass the "
                    "schema_hash from the response."
                ),
                parameter="tool_schema",
                details=f"Call ha_get_tool_details('{tool_name}') to get the current schema_hash.",
            )

        try:
            parsed_args = json.loads(args) if isinstance(args, str) else args
        except (json.JSONDecodeError, TypeError) as e:
            return create_validation_error(
                message=f"Invalid JSON in args: {e}", parameter="args",
            )
        if not isinstance(parsed_args, dict):
            return create_validation_error(
                message="args must be a JSON object.", parameter="args",
            )

        # Validate required parameters
        params_schema = tool["parameters"]
        required_params = set(params_schema.get("required", []))
        provided_params = set(parsed_args.keys())
        missing = required_params - provided_params

        if missing:
            return create_error_response(
                code=ErrorCode.VALIDATION_MISSING_PARAMETER,
                message=f"Missing required parameter(s): {', '.join(sorted(missing))}",
                context={"missing_parameters": sorted(missing)},
            )

        # Execute the tool
        try:
            implementation = tool["implementation"]
            result = await implementation(**parsed_args)
            return result

        except ToolError:
            raise  # Let ToolErrors propagate — FastMCP handles isError flag
        except TypeError as e:
            return create_validation_error(
                message=f"Parameter error: {e}",
                context={"tool_name": tool_name},
            )
        except Exception as e:
            logger.error(f"Proxy execution error for {tool_name}: {e}")
            return create_error_response(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Tool execution failed: {e}",
                context={"tool_name": tool_name},
            )
