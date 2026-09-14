"""Regression guards for pytest configuration discovery."""

from pathlib import Path

ROOT = Path(__file__).parents[3]
TESTS_ROOT = ROOT / "tests"


def test_tests_tree_has_no_nested_pytest_configuration() -> None:
    """Keep every test invocation on the root ``pyproject.toml`` config."""
    nested_configs = list(TESTS_ROOT.rglob("pytest.ini"))
    nested_configs.extend(TESTS_ROOT.rglob(".pytest.ini"))

    section_configs = {
        "pyproject.toml": "[tool.pytest.ini_options]",
        "tox.ini": "[pytest]",
        "setup.cfg": "[tool:pytest]",
    }
    for filename, section in section_configs.items():
        for path in TESTS_ROOT.rglob(filename):
            lines = path.read_text(encoding="utf-8").splitlines()
            if section in {line.strip() for line in lines}:
                nested_configs.append(path)

    relative_configs = sorted(
        path.relative_to(ROOT).as_posix() for path in nested_configs
    )
    assert not relative_configs, (
        "nested pytest configuration would override root pyproject.toml: "
        f"{relative_configs}"
    )
