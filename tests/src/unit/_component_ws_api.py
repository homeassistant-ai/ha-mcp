"""Test handle that spans the ``ha_mcp_tools`` WebSocket command modules.

The command code lives in the ``websocket_api`` package and its submodules. A
command module imports the helpers it calls by name, so the same helper is bound
in several modules. ``wsapi`` is one handle over all of them:

* Reading ``wsapi.<name>`` returns the binding from the first module that has it.
* Setting ``wsapi.<name>`` (``monkeypatch.setattr(wsapi, "<name>", ...)``) replaces
  the binding in every module that has it, so a patched seam such as
  ``_resolve_registries`` reaches each command that calls it.

Importing this module loads the component, so a test module imports it after it
has installed the ``homeassistant.*`` stubs.
"""

from __future__ import annotations

import sys
from typing import Any

from custom_components.ha_mcp_tools import const, websocket_api
from custom_components.ha_mcp_tools.const import COMPONENT_VERSION

__all__ = ["COMPONENT_VERSION", "wsapi"]


def _command_modules() -> list[Any]:
    """The ``websocket_api`` package and every loaded submodule of it."""
    return [
        module
        for name, module in sorted(sys.modules.items())
        if name == websocket_api.__name__
        or name.startswith(f"{websocket_api.__name__}.")
    ]


class _CommandSurface:
    def __getattr__(self, name: str) -> Any:
        if not name.startswith("__"):
            for module in _command_modules():
                if name in vars(module):
                    return vars(module)[name]
            if name in vars(const):
                return vars(const)[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        modules = [module for module in _command_modules() if name in vars(module)]
        if not modules:
            raise AttributeError(name)
        for module in modules:
            setattr(module, name, value)


wsapi = _CommandSurface()
