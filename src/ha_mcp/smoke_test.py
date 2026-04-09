#!/usr/bin/env python3
"""Smoke test for ha-mcp binary - verifies all dependencies are bundled correctly."""

import asyncio
import os
import sys
from typing import Any

# Force UTF-8 encoding on Windows for Unicode output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Set dummy credentials before any imports try to use them
os.environ.setdefault("HOMEASSISTANT_URL", "http://smoke-test:8123")
os.environ.setdefault("HOMEASSISTANT_TOKEN", "smoke-test-token")


def _print_errors(errors: list[str]) -> None:
    print("\n" + "=" * 60)
    print(f"SMOKE TEST FAILED: {len(errors)} error(s)")
    for error in errors:
        print(f"  - {error}")


def _test_critical_imports(errors: list[str]) -> int:
    print("\n[1/4] Testing critical library imports...")
    critical_imports = [
        ("fastmcp", "FastMCP framework"),
        ("httpx", "HTTP client"),
        ("pydantic", "Data validation"),
        ("click", "CLI framework"),
        ("websockets", "WebSocket support"),
    ]
    for module_name, description in critical_imports:
        try:
            __import__(module_name)
            print(f"  ✓ {module_name} ({description})")
        except ImportError as e:
            errors.append(f"Failed to import {module_name}: {e}")
            print(f"  ✗ {module_name} ({description}) - FAILED: {e}")
    return len(critical_imports)


def _test_server_import(errors: list[str]) -> type | None:
    print("\n[2/4] Testing server module import...")
    try:
        from ha_mcp.server import HomeAssistantSmartMCPServer
        print("  ✓ Server module imported successfully")
        return HomeAssistantSmartMCPServer
    except Exception as e:
        errors.append(f"Failed to import server module: {e}")
        print(f"  ✗ Server module import - FAILED: {e}")
        return None


def _test_server_instantiation(errors: list[str], server_cls: type) -> Any | None:
    print("\n[3/4] Testing server instantiation...")
    try:
        server = server_cls()
        mcp = server.mcp
        print(f"  ✓ Server created: {mcp.name}")
        return mcp
    except Exception as e:
        errors.append(f"Failed to create server: {e}")
        print(f"  ✗ Server instantiation - FAILED: {e}")
        return None


def _test_tool_discovery(errors: list[str], mcp: Any) -> int:
    print("\n[4/4] Testing tool discovery...")
    try:
        tools = asyncio.run(mcp.list_tools())
        tool_count = len(tools)
        print(f"  ✓ Discovered {tool_count} tools")

        if tool_count < 50:
            errors.append(f"Too few tools discovered: {tool_count} (expected 50+)")
            print("  ✗ Tool count too low (expected 50+)")
        else:
            tool_names = [t.name for t in tools[:5]]
            print(f"  ✓ Sample tools: {', '.join(tool_names)}...")
        return tool_count
    except Exception as e:
        errors.append(f"Failed to discover tools: {e}")
        print(f"  ✗ Tool discovery - FAILED: {e}")
        return 0


def main() -> int:
    """Run smoke tests and return exit code."""
    print("=" * 60)
    print("Home Assistant MCP Server - Smoke Test")
    print("=" * 60)

    errors: list[str] = []

    import_count = _test_critical_imports(errors)

    server_cls = _test_server_import(errors)
    if server_cls is None:
        _print_errors(errors)
        return 1

    mcp = _test_server_instantiation(errors, server_cls)
    if mcp is None:
        _print_errors(errors)
        return 1

    tool_count = _test_tool_discovery(errors, mcp)

    if errors:
        _print_errors(errors)
        return 1

    print("\n" + "=" * 60)
    print("SMOKE TEST PASSED: All checks successful!")
    print(f"  - All {import_count} critical libraries imported")
    print("  - Server instantiated successfully")
    print(f"  - {tool_count} tools discovered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
