"""
Category Gateway Proxy — domain-named tool groups with direct execution.

Instead of registering all tools directly with MCP (high idle context cost),
lesser-used tools are grouped into domain-named gateways:

  ha_manage_dashboards(tool="ha_config_set_dashboard", args='{"url_path": "..."}')

Each gateway's MCP description includes tool signatures with required
parameters so LLMs can call tools directly without a discovery round-trip.
Optional: call with no arguments for detailed parameter documentation.

See: https://www.anthropic.com/engineering/code-execution-with-mcp
"""

import importlib
import inspect
import json
import logging
import types
import typing
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field
from pydantic_core import PydanticUndefined

from ..errors import (
    ErrorCode,
    create_error_response,
    create_resource_not_found_error,
    create_validation_error,
)
from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Category gateway configuration.
# Maps gateway tool name → list of tool modules to include.
# To add a new gateway: add an entry here. No changes to tool modules needed.
PROXY_CATEGORIES: dict[str, list[str]] = {
    "ha_manage_dashboards": ["tools_config_dashboards", "tools_resources"],
}

# Gateway descriptions — concise summaries for the MCP tool listing.
GATEWAY_DESCRIPTIONS: dict[str, str] = {
    "ha_manage_dashboards": (
        "Create, update, delete, and customize Home Assistant dashboards "
        "including views, cards, resources, and inline code."
    ),
}


class ToolProxyRegistry:
    """Server-side registry of proxied tools and their metadata."""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
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
        category: str,
    ) -> None:
        """Register a tool in the proxy registry (NOT with MCP)."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "annotations": annotations,
            "implementation": implementation,
            "module": module,
            "category": category,
        }

        if category not in self._categories:
            self._categories[category] = []
        self._categories[category].append(name)

        logger.debug(f"Proxy registry: registered {name} (category: {category})")

    def get_tools_for_category(self, category: str) -> list[dict[str, Any]]:
        """Get all tools in a category with full details."""
        tool_names = self._categories.get(category, [])
        return [self._tools[name] for name in tool_names if name in self._tools]

    def get_tool(self, tool_name: str) -> dict[str, Any] | None:
        """Get a tool by name."""
        return self._tools.get(tool_name)

    def get_catalog(self) -> dict[str, list[str]]:
        """Get the full tool catalog grouped by category."""
        return dict(self._categories)

    def _format_parameters(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Format parameter schema into LLM-friendly list."""
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

    def build_tool_catalog(self, category: str) -> list[dict[str, Any]]:
        """Build a detailed catalog of tools in a category (for action=list)."""
        tools = self.get_tools_for_category(category)
        return [
            {
                "tool_name": tool["name"],
                "description": tool["description"],
                "parameters": self._format_parameters(tool["parameters"]),
                "required_parameters": tool["parameters"].get("required", []),
                "is_read_only": tool["annotations"].get("readOnlyHint", False),
            }
            for tool in tools
        ]

    def build_summary_lines(self, category: str) -> list[str]:
        """Build tool signatures with required params for the gateway description.

        Format: ``- tool_name(required_param, ...): First line of description``
        The ``...`` indicates optional parameters exist.  This gives LLMs enough
        info to call tools directly without a discovery round-trip.
        """
        tools = self.get_tools_for_category(category)
        lines = []
        for tool in tools:
            first_line = tool["description"].strip().split("\n")[0]

            # Build parameter signature showing required params
            params = tool["parameters"]
            properties = params.get("properties", {})
            required_set = set(params.get("required", []))

            required_names = [n for n in properties if n in required_set]
            has_optional = len(properties) > len(required_names)

            if required_names and has_optional:
                sig = ", ".join(required_names) + ", ..."
            elif required_names:
                sig = ", ".join(required_names)
            elif has_optional:
                sig = "..."
            else:
                sig = ""

            lines.append(f"- {tool['name']}({sig}): {first_line}")
        return lines


def _extract_tool_metadata(func: Any) -> tuple[str, str, dict[str, Any]]:
    """Extract tool name, description, and parameter schema from type hints."""
    name = func.__name__
    description = inspect.getdoc(func) or ""

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

        param_schema: dict[str, Any] = {"type": "string"}
        origin = typing.get_origin(hint)
        args = typing.get_args(hint)

        if origin is Annotated:
            type_to_inspect = args[0] if args else str
            for metadata in args[1:]:
                if isinstance(metadata, type(Field())):
                    field_info = metadata
                    if hasattr(field_info, "description") and field_info.description:
                        param_schema["description"] = field_info.description
                    if (
                        hasattr(field_info, "default")
                        and field_info.default is not None
                        and field_info.default is not PydanticUndefined
                    ):
                        param_schema["default"] = field_info.default
        else:
            type_to_inspect = hint

        # Extract Literal values as enum constraint
        literal_values = _extract_literal_values(type_to_inspect)
        if literal_values is not None:
            param_schema["enum"] = literal_values
            param_schema["type"] = "string"
        else:
            json_type = _python_type_to_json(type_to_inspect)
            param_schema["type"] = json_type

            if json_type == "array":
                item_schema = _get_array_items_schema(type_to_inspect)
                if item_schema:
                    param_schema["items"] = item_schema

        properties[param_name] = param_schema

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return name, description, schema


def _get_array_items_schema(python_type: Any) -> dict[str, str] | None:
    """Extract items schema for array types."""
    effective = python_type
    eff_origin = getattr(effective, "__origin__", None)

    if eff_origin is types.UnionType:
        for arg in effective.__args__:
            if arg is not type(None) and getattr(arg, "__origin__", arg) is list:
                effective = arg
                break

    type_args = getattr(effective, "__args__", ())
    if type_args:
        return {"type": _python_type_to_json(type_args[0])}
    return None


def _extract_literal_values(python_type: Any) -> list[str] | None:
    """Extract allowed values from Literal type hints (e.g., Literal["a", "b"])."""
    origin = getattr(python_type, "__origin__", None)

    # Handle Optional[Literal[...]] / Literal[...] | None
    if origin is types.UnionType:
        for arg in python_type.__args__:
            if arg is not type(None):
                result = _extract_literal_values(arg)
                if result is not None:
                    return result
        return None

    if origin is typing.Literal:
        return list(python_type.__args__)

    return None


def _python_type_to_json(python_type: Any) -> str:
    """Map Python type hints to JSON Schema types."""
    origin = getattr(python_type, "__origin__", None)

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
    return type_map.get(origin or python_type, "string")


class _MockMCP:
    """Mock FastMCP that captures @mcp.tool() registrations without registering.

    Mimics the FastMCP ``@mcp.tool()`` decorator API to intercept tool
    definitions from existing tool modules without modifying them.
    """

    def __init__(self) -> None:
        self.captured_tools: list[dict[str, Any]] = []

    def tool(self, **kwargs: Any) -> Any:
        """Capture the @mcp.tool() decorator call."""
        annotations = kwargs.get("annotations", {})

        def decorator(func: Any) -> Any:
            inner = func
            while hasattr(inner, "__wrapped__"):
                inner = inner.__wrapped__

            name, description, parameters = _extract_tool_metadata(inner)

            self.captured_tools.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                    "annotations": annotations,
                    "implementation": func,
                }
            )
            return func

        return decorator


def discover_proxy_tools(
    mcp: Any,
    client: Any,
    proxy_categories: dict[str, list[str]],
    **kwargs: Any,
) -> ToolProxyRegistry:
    """Import proxy modules and capture tool metadata without MCP registration."""
    from .registry import EXPLICIT_MODULES

    registry = ToolProxyRegistry()

    # Collect all unique modules across all categories
    module_to_categories: dict[str, list[str]] = {}
    for category, modules in proxy_categories.items():
        for module_name in modules:
            if module_name not in module_to_categories:
                module_to_categories[module_name] = []
            module_to_categories[module_name].append(category)

    for module_name, categories in module_to_categories.items():
        try:
            if module_name in EXPLICIT_MODULES:
                module = importlib.import_module(f".{module_name}", "ha_mcp.tools")
                func_name = EXPLICIT_MODULES[module_name]
                register_func = getattr(module, func_name, None)
            else:
                module = importlib.import_module(f".{module_name}", "ha_mcp.tools")
                register_func = None
                for attr_name in dir(module):
                    if attr_name.startswith("register_") and attr_name.endswith(
                        "_tools"
                    ):
                        register_func = getattr(module, attr_name)
                        break

            if not register_func:
                logger.warning(f"Proxy: no register function found in {module_name}")
                continue

            mock = _MockMCP()
            register_func(mock, client, **kwargs)

            # Register tools in all categories this module belongs to
            for tool_info in mock.captured_tools:
                for category in categories:
                    registry.register_tool(
                        name=tool_info["name"],
                        description=tool_info["description"],
                        parameters=tool_info["parameters"],
                        annotations=tool_info["annotations"],
                        implementation=tool_info["implementation"],
                        module=module_name,
                        category=category,
                    )

            logger.debug(
                f"Proxy: captured {len(mock.captured_tools)} tools from {module_name}"
            )

        except Exception as e:
            logger.error(f"Proxy: failed to capture tools from {module_name}: {e}")
            raise

    logger.info(
        f"Tool proxy registry: {registry.tool_count} tools in "
        f"{len(proxy_categories)} categories"
    )
    return registry


def register_category_gateways(
    mcp: Any,
    client: Any,
    proxy_registry: ToolProxyRegistry,
    proxy_categories: dict[str, list[str]],
    **kwargs: Any,
) -> None:
    """Register one MCP gateway tool per category.

    Each gateway combines discovery (action=list) and execution (action=execute)
    into a single tool, replacing the 3-step find→details→execute pattern.
    """
    for category_name in proxy_categories:
        _register_single_gateway(mcp, proxy_registry, category_name)


def _register_single_gateway(
    mcp: Any,
    proxy_registry: ToolProxyRegistry,
    category_name: str,
) -> None:
    """Register a single category gateway tool with MCP."""
    summary_lines = proxy_registry.build_summary_lines(category_name)
    tools_list = "\n".join(summary_lines)

    gateway_summary = GATEWAY_DESCRIPTIONS.get(category_name, "")
    description = (
        f"{gateway_summary}\n\n"
        f"Available tools:\n{tools_list}\n\n"
        f"Call with tool='<name>' and args='{{\"param\": \"value\"}}' to execute.\n"
        f"Optionally call with no arguments for detailed parameter help."
    )

    # Determine if any sub-tool is destructive
    tools_in_category = proxy_registry.get_tools_for_category(category_name)
    has_destructive = any(
        not t["annotations"].get("readOnlyHint", False) for t in tools_in_category
    )

    @mcp.tool(
        name=category_name,
        description=description,
        annotations={
            "destructiveHint": has_destructive,
            "title": category_name.replace("ha_", "").replace("_", " ").title(),
        },
    )
    @log_tool_usage
    async def gateway_handler(
        tool: Annotated[
            str | None,
            Field(
                default=None,
                description=("Tool name to execute. Omit for detailed parameter help."),
            ),
        ] = None,
        args: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "JSON object of arguments to pass to the tool. "
                    "Required when tool is specified."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        # List mode: return full catalog with parameter schemas
        if tool is None:
            catalog = proxy_registry.build_tool_catalog(category_name)
            return {
                "success": True,
                "category": category_name,
                "tools": catalog,
                "count": len(catalog),
                "usage": (
                    f"Call {category_name}(tool='<tool_name>', "
                    f'args=\'{{"param": "value"}}\') to execute a tool.'
                ),
            }

        # Execute mode: validate and run the tool
        tool_entry = proxy_registry.get_tool(tool)
        if not tool_entry:
            available = [
                t["name"] for t in proxy_registry.get_tools_for_category(category_name)
            ]
            return create_resource_not_found_error(
                resource_type="Tool",
                identifier=tool,
                details=(
                    f"Tool '{tool}' not found in {category_name}. "
                    f"Available: {', '.join(available)}"
                ),
            )

        # Verify tool belongs to this category
        if tool_entry["category"] != category_name:
            return create_validation_error(
                message=f"Tool '{tool}' does not belong to {category_name}.",
                parameter="tool",
            )

        # Parse args
        if args is None:
            parsed_args: dict[str, Any] = {}
        else:
            try:
                parsed_args = json.loads(args) if isinstance(args, str) else args
            except (json.JSONDecodeError, TypeError) as e:
                return create_validation_error(
                    message=f"Invalid JSON in args: {e}",
                    parameter="args",
                )

        if not isinstance(parsed_args, dict):
            return create_validation_error(
                message="args must be a JSON object.",
                parameter="args",
            )

        # Validate required parameters
        params_schema = tool_entry["parameters"]
        required_params = set(params_schema.get("required", []))
        provided_params = set(parsed_args.keys())
        missing = required_params - provided_params

        if missing:
            return create_error_response(
                code=ErrorCode.VALIDATION_MISSING_PARAMETER,
                message=f"Missing required parameter(s): {', '.join(sorted(missing))}",
                context={"missing_parameters": sorted(missing)},
            )

        # Validate enum constraints (Literal types) — mirrors FastMCP's
        # schema-level validation which the gateway bypasses.
        properties = params_schema.get("properties", {})
        for param_name, value in parsed_args.items():
            prop = properties.get(param_name, {})
            allowed = prop.get("enum")
            if allowed is not None and value not in allowed:
                raise ToolError(
                    f"Invalid value '{value}' for parameter '{param_name}'. "
                    f"Allowed values: {allowed}"
                )

        # Execute
        try:
            implementation = tool_entry["implementation"]
            result = await implementation(**parsed_args)
            return result

        except ToolError:
            raise
        except TypeError as e:
            return create_validation_error(
                message=f"Parameter error: {e}",
                context={"tool_name": tool},
            )
        except Exception as e:
            logger.error(f"Gateway execution error for {tool}: {e}")
            return create_error_response(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Tool execution failed: {e}",
                context={"tool_name": tool},
            )
