"""Regression guard: every ``tools_*.py`` module must register tools.

The registry (``ha_mcp.tools.registry``) auto-discovers modules by the
``tools_*.py`` filename convention and imports each one expecting a
``register_*_tools`` function. A module that matches the convention but
exposes no such function logs a misleading
``Module <name> has no register_*_tools function`` WARNING on every
startup — even though nothing is actually broken (it's typically a
shared-machinery module that merely happens to carry the ``tools_``
prefix).

This test enforces the convention's contract so a machinery module is
never named ``tools_*``: pure helper modules belong under a non-``tools_``
name (e.g. ``config_entry_flow.py``) and are imported explicitly by the
tool modules that use them.
"""

from __future__ import annotations

import importlib
import pkgutil

import ha_mcp.tools as tools_pkg


def _discovered_tools_modules() -> list[str]:
    """Mirror the registry's filename-convention discovery."""
    return [
        info.name
        for info in pkgutil.iter_modules(tools_pkg.__path__)
        if info.name.startswith("tools_")
    ]


def test_every_tools_module_has_a_register_function() -> None:
    offenders: list[str] = []
    for module_name in _discovered_tools_modules():
        module = importlib.import_module(f"ha_mcp.tools.{module_name}")
        has_register = any(
            attr.startswith("register_")
            and attr.endswith("_tools")
            and callable(getattr(module, attr))
            for attr in dir(module)
        )
        if not has_register:
            offenders.append(module_name)

    assert not offenders, (
        "These modules match the tools_*.py convention but expose no "
        "register_*_tools function, so the registry logs a misleading "
        f"startup WARNING for each: {offenders}. Machinery-only modules "
        "must not carry the tools_ prefix — rename them (e.g. "
        "config_entry_flow.py) and import them explicitly."
    )
