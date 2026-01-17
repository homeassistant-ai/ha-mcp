#!/usr/bin/env python3
"""Smoke test for ha-mcp binary - verifies all dependencies are bundled correctly."""

import os
import sys

# Force UTF-8 encoding on Windows for Unicode output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Set dummy credentials before any imports try to use them
os.environ.setdefault("HOMEASSISTANT_URL", "http://smoke-test:8123")
os.environ.setdefault("HOMEASSISTANT_TOKEN", "smoke-test-token")


def main() -> int:
    """Run smoke tests and return exit code."""
    print("=" * 60)
    print("Home Assistant MCP Server - Smoke Test")
    print("=" * 60)

    errors = []

    # Test 1: Critical library imports
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

    # Test 2: Server module import
    print("\n[2/4] Testing server module import...")
    try:
        from ha_mcp.server import HomeAssistantSmartMCPServer
        print("  ✓ Server module imported successfully")
    except Exception as e:
        errors.append(f"Failed to import server module: {e}")
        print(f"  ✗ Server module import - FAILED: {e}")
        # Can't continue if server module fails
        print("\n" + "=" * 60)
        print(f"SMOKE TEST FAILED: {len(errors)} error(s)")
        for error in errors:
            print(f"  - {error}")
        return 1

    # Test 3: Server instantiation
    print("\n[3/4] Testing server instantiation...")
    try:
        server = HomeAssistantSmartMCPServer()
        mcp = server.mcp
        print(f"  ✓ Server created: {mcp.name}")
    except Exception as e:
        errors.append(f"Failed to create server: {e}")
        print(f"  ✗ Server instantiation - FAILED: {e}")
        # Can't continue if server creation fails
        print("\n" + "=" * 60)
        print(f"SMOKE TEST FAILED: {len(errors)} error(s)")
        for error in errors:
            print(f"  - {error}")
        return 1

    # Test 4: Tool discovery
    print("\n[4/4] Testing tool discovery...")
    try:
        # Access the tools from the MCP instance
        tool_count = len(mcp._tool_manager._tools)
        print(f"  ✓ Discovered {tool_count} tools")

        if tool_count < 50:
            errors.append(f"Too few tools discovered: {tool_count} (expected 50+)")
            print("  ✗ Tool count too low (expected 50+)")
        else:
            # List a few tool names as examples
            tool_names = list(mcp._tool_manager._tools.keys())[:5]
            print(f"  ✓ Sample tools: {', '.join(tool_names)}...")
    except Exception as e:
        errors.append(f"Failed to discover tools: {e}")
        print(f"  ✗ Tool discovery - FAILED: {e}")

    # Summary
    print("\n" + "=" * 60)
    if errors:
        print(f"SMOKE TEST FAILED: {len(errors)} error(s)")
        for error in errors:
            print(f"  - {error}")
        return 1
    else:
        print("SMOKE TEST PASSED: All checks successful!")
        print(f"  - All {len(critical_imports)} critical libraries imported")
        print("  - Server instantiated successfully")
        print(f"  - {tool_count} tools discovered")
        return 0


if __name__ == "__main__":
    sys.exit(main())
