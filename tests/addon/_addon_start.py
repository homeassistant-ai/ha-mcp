"""Load the app's ``start.py``, a script rather than a package module."""

import functools
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


@functools.cache
def load_addon_start() -> ModuleType:
    """Import homeassistant-addon/start.py as a module."""
    app_dir = Path(__file__).parents[2] / "homeassistant-addon"
    spec = importlib.util.spec_from_file_location("addon_start", app_dir / "start.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # In the image start.py imports its sibling modules from ``/``.
    sys.path.insert(0, str(app_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(app_dir))
    return module
