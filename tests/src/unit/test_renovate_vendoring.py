"""Guard the commands Renovate's post-upgrade tasks may run."""

import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_PIN_FILE = "src/ha_mcp/_vendor/requirements.txt"
_WEBSOCKETS_COMMAND = "python3 -I scripts/vendor_websockets.py"
_FASTMCP_COMMAND = (
    "uv run --no-project --with packaging python -I scripts/vendor_fastmcp.py"
)
_LOCK_SYNC_COMMAND = "uv run --no-project --with packaging python -I scripts/check_ha_constraint_alignment.py --sync-lock"
_PIN_SYNC_COMMAND = "uv run --no-project --with packaging python -I scripts/sync_dev_integration_pins.py"


class _Hook(NamedTuple):
    packages: list[str]
    commands: list[str]
    file_filters: list[str]
    tools: dict[str, dict[str, str]]
    python: str


_HOOKS = {
    _WEBSOCKETS_COMMAND: _Hook(
        packages=["websockets"],
        commands=[_WEBSOCKETS_COMMAND],
        file_filters=["src/ha_mcp/_vendor/websockets/**"],
        tools={"python": {}},
        python=">=3.14,<3.15",
    ),
    # The generator raises pyproject.toml floors, so the lock must follow; uv
    # lock resolves the dev group's homeassistant, which needs Python 3.14.2 or later.
    _FASTMCP_COMMAND: _Hook(
        packages=["fastmcp-slim", "mcp", "mcp-types"],
        commands=[_FASTMCP_COMMAND, "uv lock"],
        file_filters=[
            "src/ha_mcp/_vendor/fastmcp/**",
            "src/ha_mcp/_vendor/mcp/**",
            "src/ha_mcp/_vendor/mcp_types/**",
            "pyproject.toml",
            "uv.lock",
        ],
        tools={"python": {}, "uv": {}},
        python=">=3.14.2,<3.15",
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
        if "postUpgradeTasks" in rule and rule.get("matchFileNames") == [_PIN_FILE]
    }
    assert set(rules) == set(_HOOKS), "one regeneration hook per vendoring script"
    for command, hook in _HOOKS.items():
        rule = rules[command]
        assert rule["matchManagers"] == ["custom.regex"]
        assert rule["matchDatasources"] == ["pypi"]
        assert rule["matchPackageNames"] == hook.packages
        assert rule["matchFileNames"] == [_PIN_FILE]
        assert rule["postUpgradeTasks"]["commands"] == hook.commands
        assert rule["postUpgradeTasks"]["fileFilters"] == hook.file_filters
        assert rule["postUpgradeTasks"]["installTools"] == hook.tools
        assert rule["constraints"]["python"] == hook.python
        # Update mode retains the matched upgrade's Python constraint in 44.50.1.
        assert rule["postUpgradeTasks"]["executionMode"] == "update"
        assert "minimumReleaseAge" not in rule
        assert "schedule" not in rule


def test_vendored_fastmcp_packages_update_together() -> None:
    config, _ = _configuration()
    rule = next(
        r
        for r in config["packageRules"]
        if _FASTMCP_COMMAND in r.get("postUpgradeTasks", {}).get("commands", [])
    )
    assert rule["groupName"] == "vendored fastmcp"


@pytest.mark.parametrize(
    "command",
    [
        _WEBSOCKETS_COMMAND + " --extra",
        _FASTMCP_COMMAND + " --extra",
        _WEBSOCKETS_COMMAND + "; echo unsafe",
        "echo unsafe && " + _FASTMCP_COMMAND,
        "python3 -I scripts/vendor_websocketsXpy",
        "uv run --no-project --with packaging python -I scripts/vendor_fastmcpXpy",
        "python3 scripts/vendor_websockets.py",
        "python3 scripts/vendor_fastmcp.py",
        "python3 -I scripts/vendor_fastmcp.py",
        "uv run --no-project --with packaging python scripts/vendor_fastmcp.py",
        "python3 -I scripts/another_script.py",
        "uv lock --upgrade",
        "uv lock; echo unsafe",
        _LOCK_SYNC_COMMAND + " --extra",
        "uv run --no-project --with packaging python -I scripts/other.py",
        _PIN_SYNC_COMMAND + " --extra",
    ],
)
def test_a_variant_of_a_post_upgrade_command_is_not_authorized(command: str) -> None:
    _, env = _configuration()
    patterns = json.loads(env.get("RENOVATE_ALLOWED_COMMANDS", "[]"))
    assert not any(re.search(pattern, command) for pattern in patterns)
    assert env.get("RENOVATE_ALLOW_SHELL_EXECUTOR_FOR_POST_UPGRADE_COMMANDS") == "false"


def test_every_configured_post_upgrade_command_is_authorized() -> None:
    # Renovate refuses a command the scanner does not allow, and the update
    # then carries an artifact error instead of the regenerated files.
    config, env = _configuration()
    patterns = json.loads(env["RENOVATE_ALLOWED_COMMANDS"])
    commands = [
        command
        for rule in config["packageRules"]
        for command in rule.get("postUpgradeTasks", {}).get("commands", [])
    ]
    assert {
        _WEBSOCKETS_COMMAND,
        _FASTMCP_COMMAND,
        _PIN_SYNC_COMMAND,
        "uv lock",
        _LOCK_SYNC_COMMAND,
    } <= set(commands)
    for command in commands:
        assert any(re.search(pattern, command) for pattern in patterns), command


def test_core_bump_syncs_integration_pins_before_it_locks() -> None:
    # uv lock reads pyproject.toml, so pins synced after it would leave the
    # lock on the old Core's versions.
    config, _ = _configuration()
    tasks = next(
        rule["postUpgradeTasks"]
        for rule in config["packageRules"]
        if _PIN_SYNC_COMMAND in rule.get("postUpgradeTasks", {}).get("commands", [])
    )
    commands = tasks["commands"]
    assert commands.index(_PIN_SYNC_COMMAND) < commands.index("uv lock")
    assert {"pyproject.toml", ".github/dependabot.yml", "uv.lock"} <= set(
        tasks["fileFilters"]
    )
