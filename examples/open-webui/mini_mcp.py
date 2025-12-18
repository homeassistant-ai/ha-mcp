#!/usr/bin/env python3
"""
Minimal MCP server for testing tool calling with local LLMs.
Uses fastmcp to create a simple server with 2 basic tools.

This helps diagnose if tool calling issues are:
- Model-related (can't call ANY tools)
- ha-mcp specific (too many/complex tools)

Run with:
  uvx --from fastmcp fastmcp run mini_mcp.py

Or via stdio:
  python mini_mcp.py
"""
from datetime import datetime
from fastmcp import FastMCP

mcp = FastMCP("Mini Test MCP")


@mcp.tool()
def get_time() -> str:
    """Get the current time and date."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together.

    Args:
        a: First number
        b: Second number

    Returns:
        Sum of a and b
    """
    return a + b


@mcp.tool()
def greet(name: str) -> str:
    """Say hello to someone.

    Args:
        name: The person's name to greet

    Returns:
        A friendly greeting
    """
    return f"Hello, {name}! Nice to meet you."


if __name__ == "__main__":
    mcp.run()
