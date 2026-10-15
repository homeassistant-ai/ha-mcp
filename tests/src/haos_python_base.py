"""The bake's python-base registry fallback, for the test-time app refresh.

``refresh_dev_addon_source_in_qcow2`` restages the dev app and Supervisor
rebuilds it with ``--pull``, which meets ECR Public's rate limit just as the
bake does; it takes the same Dockerfile rewrite. Loaded by path because
``tests/haos_image_build`` is not importable from the E2E runtime.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "haos_image_build" / "python_base.py"
_spec = importlib.util.spec_from_file_location("haos_image_python_base", _PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

rewrite_python_base = _module.rewrite_python_base
