"""Production code has no public name that only the tests use.

``scripts/test_only_public_names.py`` runs here on a small repository written
for each test, and once on this checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_only_public_names.py"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_only_public_names", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, production: str, test: str) -> Path:
    for name, text in {
        "src/ha_mcp/tools/helpers.py": production,
        "src/ha_mcp/tools/caller.py": "from .helpers import used\n\nused()\n",
        "tests/src/unit/test_helpers.py": test,
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("production", "refused"),
    [
        pytest.param("def helper():\n    pass\n", True, id="function"),
        pytest.param("class Helper:\n    pass\n", True, id="class"),
        pytest.param(
            "class Tools:\n    def helper(self):\n        pass\n", True, id="method"
        ),
        pytest.param("HELPER = 1\n", True, id="constant"),
        pytest.param("def _helper():\n    pass\n", False, id="private"),
        pytest.param("def used():\n    pass\n", False, id="called-by-production"),
        pytest.param(
            "def register_helper_tools(mcp):\n    pass\n", False, id="tool-registration"
        ),
        pytest.param(
            "async def async_setup_entry(hass, entry):\n    pass\n", False, id="ha-hook"
        ),
        pytest.param("@decorator\ndef helper():\n    pass\n", False, id="decorated"),
        pytest.param(
            "class Flow(Base, domain='x'):\n    pass\n", False, id="config-flow"
        ),
    ],
)
def test_a_public_name_only_the_tests_use_is_refused(
    guard: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    production: str,
    refused: bool,
) -> None:
    test = (
        "from ha_mcp.tools.helpers import helper, Helper, HELPER, _helper, used\n"
        "Tools().helper()\nregister_helper_tools\nasync_setup_entry\nFlow\n"
    )
    repo = _repo(tmp_path, production, test)

    assert guard.main(["--repo", str(repo)]) == (1 if refused else 0)
    output = capsys.readouterr().out
    assert ("src/ha_mcp/tools/helpers.py:" in output) is refused


def test_a_name_no_test_uses_is_left_to_the_dead_code_checks(
    guard: ModuleType, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, "def unused():\n    pass\n", "")

    assert guard.main(["--repo", str(repo)]) == 0


def test_the_module_state_the_tests_reset_is_exempt(
    guard: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "src/ha_mcp/config.py"
    path.parent.mkdir(parents=True)
    path.write_text("def reset_embedded_connection():\n    pass\n", encoding="utf-8")
    test = tmp_path / "tests/test_config.py"
    test.parent.mkdir(parents=True)
    test.write_text("config.reset_embedded_connection()\n", encoding="utf-8")

    assert guard.main(["--repo", str(tmp_path)]) == 0


def _files(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("files", "refused"),
    [
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/b.py": "def run(device):\n    device.probe()\n",
            },
            True,
            id="unrelated-attribute-of-the-same-name",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/b.py": "from . import a\n\na.probe()\n",
            },
            False,
            id="called-through-its-module",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": '__all__ = ["probe"]\ndef probe():\n    pass\n'
                'globals()["probe"]()\n',
            },
            False,
            id="looked-up-by-a-string-it-also-exports",
        ),
        pytest.param(
            {
                "src/ha_mcp/tools/a.py": "def probe():\n    pass\n",
                "scripts/tool.py": "a = module_from_spec(spec)\na.probe()\n",
            },
            False,
            id="called-through-a-module-a-script-loads-by-path",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/b.py": "def run(a):\n    a.probe()\n",
            },
            True,
            id="parameter-named-like-the-module",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/b.py": "def other():\n    pass\n",
                "src/ha_mcp/c.py": "from . import a as api\n\napi.probe()\n\n\n"
                "def run():\n    from . import b as api\n\n    api.other()\n",
            },
            False,
            id="alias-bound-again-inside-a-function",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/b.py": 'def run():\n    """Calls ``probe``."""\n',
            },
            True,
            id="named-only-in-a-docstring",
        ),
        pytest.param(
            {"src/ha_mcp/a.py": '__all__ = ["probe"]\n\n\ndef probe():\n    pass\n'},
            True,
            id="named-only-in-all",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/b.py": "from . import a\n\nHANDLERS = {'x': a}\n\n\n"
                "def run(kind):\n    handler = HANDLERS[kind]\n    handler.probe()\n",
            },
            False,
            id="read-through-a-receiver-holding-its-imported-module",
        ),
        pytest.param(
            {
                "src/ha_mcp/x/a.py": "def probe():\n    pass\n",
                "src/ha_mcp/y/b.py": "from ..x import a\n\na.probe()\n",
            },
            False,
            id="imported-two-packages-up",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "homeassistant-addon/start.py": "from ha_mcp.a import probe\n\nprobe()\n",
            },
            False,
            id="called-by-an-app",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "def probe():\n    pass\n",
                "pyproject.toml": '[project.scripts]\nx = "ha_mcp.a:probe"\n',
            },
            False,
            id="named-by-a-file-production-reads",
        ),
        pytest.param(
            {
                "src/ha_mcp/a.py": "class Tool(Base):\n    def probe(self):\n        pass\n"
            },
            False,
            id="method-of-a-class-with-a-base",
        ),
    ],
)
def test_only_a_use_of_the_definition_itself_counts(
    guard: ModuleType, tmp_path: Path, files: dict[str, str], refused: bool
) -> None:
    repo = _files(tmp_path, {**files, "tests/test_a.py": "from x import probe\n"})

    assert guard.main(["--repo", str(repo)]) == (1 if refused else 0)


def test_this_checkout_has_no_public_name_only_the_tests_use(
    guard: ModuleType,
) -> None:
    assert guard.main([]) == 0
