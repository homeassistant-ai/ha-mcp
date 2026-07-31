"""Regenerate the English-source baseline that guards translation drift.

``tests/src/unit/test_locale_parity.py`` pins the English string every
translation was written against. ``scripts/translate_locales.py`` repins it
automatically after a machine-translation pass; run this directly after a
*hand*-translation pass, once every locale carrying the changed key has been
updated or confirmed:

    python scripts/update_locale_baseline.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_MODULE = _REPO_ROOT / "tests" / "src" / "unit" / "test_locale_parity.py"


def _load_test_module() -> ModuleType:
    """Import the check's own helpers so both sides hash identically."""
    spec = importlib.util.spec_from_file_location("_locale_parity", _TEST_MODULE)
    if spec is None or spec.loader is None:  # pragma: no cover - path guard
        raise SystemExit(f"cannot import {_TEST_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = _load_test_module()
    module.BASELINE_PATH.write_text(
        json.dumps(module.english_sources(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {module.BASELINE_PATH.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
