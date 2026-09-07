"""Guard the executable boundary for automated vendored-library updates."""

import json
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_COMMAND = "python3 -I scripts/vendor_websockets.py"


def _configuration() -> tuple[dict, dict]:
    config = json.loads((_ROOT / "renovate.json").read_text())
    workflow = yaml.safe_load((_ROOT / ".github/workflows/renovate.yml").read_text())
    action = next(
        step
        for step in workflow["jobs"]["renovate"]["steps"]
        if step.get("name") == "Self-hosted Renovate"
    )
    return config, action["env"]


def test_vendoring_hook_is_scoped_to_the_private_websockets_pin() -> None:
    config, _ = _configuration()
    rules = [rule for rule in config["packageRules"] if "postUpgradeTasks" in rule]
    assert len(rules) == 1, "the vendored pin needs one regeneration hook"
    rule = rules[0]
    assert rule["matchManagers"] == ["custom.regex"]
    assert rule["matchDatasources"] == ["pypi"]
    assert rule["matchPackageNames"] == ["websockets"]
    assert rule["matchFileNames"] == ["src/ha_mcp/_vendor/requirements.txt"]
    assert rule["postUpgradeTasks"]["commands"] == [_COMMAND]
    assert rule["postUpgradeTasks"]["fileFilters"] == [
        "src/ha_mcp/_vendor/websockets/**"
    ]
    assert rule["postUpgradeTasks"]["installTools"] == {"python": {}}
    assert rule["constraints"]["python"] == ">=3.13,<3.14"
    # Update mode retains the matched upgrade's Python constraint in 44.50.1.
    assert rule["postUpgradeTasks"]["executionMode"] == "update"
    assert "minimumReleaseAge" not in rule
    assert "schedule" not in rule


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        (_COMMAND, True),
        (_COMMAND + " --extra", False),
        (_COMMAND + "; echo unsafe", False),
        ("echo unsafe && " + _COMMAND, False),
        ("python3 -I scripts/vendor_websocketsXpy", False),
        ("python3 scripts/vendor_websockets.py", False),
        ("python3 -I scripts/another_script.py", False),
    ],
)
def test_only_the_exact_vendoring_command_is_authorized(
    command: str, allowed: bool
) -> None:
    _, env = _configuration()
    patterns = json.loads(env.get("RENOVATE_ALLOWED_COMMANDS", "[]"))
    assert any(re.search(pattern, command) for pattern in patterns) is allowed
    assert env.get("RENOVATE_ALLOW_SHELL_EXECUTOR_FOR_POST_UPGRADE_COMMANDS") == "false"
