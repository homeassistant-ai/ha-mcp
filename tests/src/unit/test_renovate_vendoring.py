"""Guard the executable boundary for automated vendored-library updates."""

import json
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_PIN_FILE = "src/ha_mcp/_vendor/requirements.txt"
_WEBSOCKETS_COMMAND = "python3 -I scripts/vendor_websockets.py"
_FASTMCP_COMMAND = "python3 -I scripts/vendor_fastmcp.py"
_HOOKS = {
    _WEBSOCKETS_COMMAND: (["websockets"], ["src/ha_mcp/_vendor/websockets/**"]),
    _FASTMCP_COMMAND: (
        ["fastmcp-slim", "mcp", "mcp-types"],
        [
            "src/ha_mcp/_vendor/fastmcp/**",
            "src/ha_mcp/_vendor/mcp/**",
            "src/ha_mcp/_vendor/mcp_types/**",
        ],
    ),
}


def _configuration() -> tuple[dict, dict]:
    config = json.loads((_ROOT / "renovate.json").read_text())
    workflow = yaml.safe_load((_ROOT / ".github/workflows/renovate.yml").read_text())
    action = next(
        step
        for step in workflow["jobs"]["renovate"]["steps"]
        if step.get("name") == "Self-hosted Renovate"
    )
    return config, action["env"]


def test_each_vendoring_hook_is_scoped_to_its_private_pins() -> None:
    config, _ = _configuration()
    rules = {
        rule["postUpgradeTasks"]["commands"][0]: rule
        for rule in config["packageRules"]
        if "postUpgradeTasks" in rule
    }
    assert set(rules) == set(_HOOKS), "one regeneration hook per vendoring script"
    for command, (packages, file_filters) in _HOOKS.items():
        rule = rules[command]
        assert rule["matchManagers"] == ["custom.regex"]
        assert rule["matchDatasources"] == ["pypi"]
        assert rule["matchPackageNames"] == packages
        assert rule["matchFileNames"] == [_PIN_FILE]
        assert rule["postUpgradeTasks"]["commands"] == [command]
        assert rule["postUpgradeTasks"]["fileFilters"] == file_filters
        assert rule["postUpgradeTasks"]["installTools"] == {"python": {}}
        assert rule["constraints"]["python"] == ">=3.13,<3.14"
        # Update mode retains the matched upgrade's Python constraint in 44.50.1.
        assert rule["postUpgradeTasks"]["executionMode"] == "update"
        assert "minimumReleaseAge" not in rule
        assert "schedule" not in rule


def test_vendored_fastmcp_packages_update_together() -> None:
    config, _ = _configuration()
    rule = next(
        r
        for r in config["packageRules"]
        if r.get("postUpgradeTasks", {}).get("commands") == [_FASTMCP_COMMAND]
    )
    assert rule["groupName"] == "vendored fastmcp"


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        (_WEBSOCKETS_COMMAND, True),
        (_FASTMCP_COMMAND, True),
        (_WEBSOCKETS_COMMAND + " --extra", False),
        (_FASTMCP_COMMAND + " --extra", False),
        (_WEBSOCKETS_COMMAND + "; echo unsafe", False),
        ("echo unsafe && " + _FASTMCP_COMMAND, False),
        ("python3 -I scripts/vendor_websocketsXpy", False),
        ("python3 -I scripts/vendor_fastmcpXpy", False),
        ("python3 scripts/vendor_websockets.py", False),
        ("python3 scripts/vendor_fastmcp.py", False),
        ("python3 -I scripts/another_script.py", False),
    ],
)
def test_only_the_exact_vendoring_commands_are_authorized(
    command: str, allowed: bool
) -> None:
    _, env = _configuration()
    patterns = json.loads(env.get("RENOVATE_ALLOWED_COMMANDS", "[]"))
    assert any(re.search(pattern, command) for pattern in patterns) is allowed
    assert env.get("RENOVATE_ALLOW_SHELL_EXECUTOR_FOR_POST_UPGRADE_COMMANDS") == "false"
