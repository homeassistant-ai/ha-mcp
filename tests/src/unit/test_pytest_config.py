"""Regression guards for pytest configuration discovery."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[3]
TESTS_ROOT = ROOT / "tests"


def test_tests_tree_has_no_nested_pytest_configuration() -> None:
    """Keep every test invocation on the root ``pyproject.toml`` config."""
    always_config_names = (
        "pytest.toml",
        ".pytest.toml",
        "pytest.ini",
        ".pytest.ini",
    )
    nested_configs = [
        path for filename in always_config_names for path in TESTS_ROOT.rglob(filename)
    ]

    for path in TESTS_ROOT.rglob("pyproject.toml"):
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        tool_config = config.get("tool", {})
        if isinstance(tool_config, dict) and "pytest" in tool_config:
            nested_configs.append(path)

    section_configs = {
        "tox.ini": "[pytest]",
        "setup.cfg": "[tool:pytest]",
    }
    for filename, section in section_configs.items():
        for path in TESTS_ROOT.rglob(filename):
            lines = path.read_text(encoding="utf-8").splitlines()
            headers = {line.split("#", 1)[0].split(";", 1)[0].strip() for line in lines}
            if section in headers:
                nested_configs.append(path)

    relative_configs = sorted(
        path.relative_to(ROOT).as_posix() for path in nested_configs
    )
    assert not relative_configs, (
        "nested pytest configuration would override root pyproject.toml: "
        f"{relative_configs}"
    )
