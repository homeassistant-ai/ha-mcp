"""Load the app's ``start.py``, a script rather than a package module."""

import functools
import importlib.util
from pathlib import Path
from types import ModuleType


@functools.cache
def load_addon_start() -> ModuleType:
    """Import homeassistant-addon/start.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "addon_start",
        Path(__file__).parents[2] / "homeassistant-addon" / "start.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
