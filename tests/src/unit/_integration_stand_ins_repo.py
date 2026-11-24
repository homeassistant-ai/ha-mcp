"""What the tests of ``scripts/integration_stand_ins.py`` share: the script,
loaded once, and a small repository to run it on."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "integration_stand_ins.py"
SAMPLE = "tests/src/unit/test_sample.py"
LISTED = f"- file: {SAMPLE}\n  integration: hassio\n  reason: hassio only loads it\n"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("integration_stand_ins", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_repo(tmp_path: Path, files: dict[str, str], allowlist: str = "[]\n") -> Path:
    for tree in ("tests/src/unit", "tests/addon"):
        (tmp_path / tree).mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "tests/src/unit/integration_stand_in_allowlist.yml").write_text(
        allowlist, encoding="utf-8"
    )
    return tmp_path
