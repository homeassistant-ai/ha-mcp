"""Every tool the real ToolsRegistry could register, for the tool-wide guards.

Kept out of the test modules so each guard can import it without importing
another test module.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import pathlib
import pkgutil
import tempfile
from typing import Any
from unittest.mock import MagicMock

import ha_mcp.tools as tools_pkg


def _register_module(mcp: Any, module: Any, func_name: str | None) -> None:
    """Call a module's register function (explicit name, or discovered
    register_*_tools), mirroring ToolsRegistry._import_and_register_module."""
    if func_name is not None:
        register_fn: Any = getattr(module, func_name, None)
    else:
        register_fn = next(
            (
                getattr(module, attr)
                for attr in dir(module)
                if attr.startswith("register_")
                and attr.endswith("_tools")
                and callable(getattr(module, attr))
            ),
            None,
        )
    if register_fn is not None:
        # Same kwargs ToolsRegistry.register_all builds — including
        # ``server``, which register_security_policy_tools reads strictly
        # (kwargs["server"]) because the registry always supplies it.
        register_fn(
            mcp,
            MagicMock(),
            smart_tools=MagicMock(),
            device_tools=MagicMock(),
            server=MagicMock(),
        )


def all_registered_tools() -> dict[str, Any]:
    """Register EVERY tool the real ToolsRegistry could register and return them.

    Mirrors ToolsRegistry: walks the tools_*.py modules AND the EXPLICIT_MODULES
    (e.g. backup.py, which has no tools_ prefix). Every bool feature flag is
    forced on — except read_only_mode, which would *hide* write tools — so that
    beta/gated modules register and their params are inspected; otherwise a
    container param on a beta tool would silently escape the guardrail in default
    CI, where the beta master toggle is off. Env is restored afterward.
    """
    from ha_mcp._vendor.fastmcp import FastMCP
    from ha_mcp.config import FEATURE_FLAG_FIELDS, reset_global_settings
    from ha_mcp.tools.registry import EXPLICIT_MODULES
    from ha_mcp.utils.data_paths import get_data_dir

    async def _inner() -> dict[str, Any]:
        mcp = FastMCP("guardrail")
        for module_info in pkgutil.iter_modules(tools_pkg.__path__):
            if module_info.name.startswith("tools_"):
                module = importlib.import_module(f"ha_mcp.tools.{module_info.name}")
                _register_module(mcp, module, None)
        for module_name, func_name in EXPLICIT_MODULES.items():
            module = importlib.import_module(f"ha_mcp.tools.{module_name}")
            _register_module(mcp, module, func_name)
        listed = await mcp.list_tools()
        return {t.name: await mcp.get_tool(t.name) for t in listed}

    # Everything this function mutates -- the feature-flag variables, the config
    # directory, the two caches -- is set inside the try, so a failure between
    # here and the run cannot leave the next test with a modified environment.
    saved_env: dict[str, str | None] = {}
    isolated_config = tempfile.TemporaryDirectory()
    try:
        for flag in FEATURE_FLAG_FIELDS:
            if flag.ftype is bool and flag.field != "read_only_mode":
                saved_env[flag.env] = os.environ.get(flag.env)
                os.environ[flag.env] = "true"

        # Point the settings at an empty directory: otherwise
        # get_global_settings() reads the real data dir, and a tool_config.json
        # left there by a local run decides which tools this guardrail gets to
        # inspect. Clearing the cache is the half that does the work --
        # get_data_dir is lru_cached, so setting the variable alone changes
        # nothing once an earlier test has resolved it.
        saved_env["HA_MCP_CONFIG_DIR"] = os.environ.get("HA_MCP_CONFIG_DIR")
        os.environ["HA_MCP_CONFIG_DIR"] = isolated_config.name
        get_data_dir.cache_clear()
        reset_global_settings()
        assert get_data_dir() == pathlib.Path(isolated_config.name), (
            "the isolation did not take: the guardrail would read persisted "
            "local state instead of an empty directory"
        )

        return asyncio.run(_inner())
    finally:
        for env_var, prev in saved_env.items():
            if prev is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = prev
        get_data_dir.cache_clear()
        reset_global_settings()
        isolated_config.cleanup()
